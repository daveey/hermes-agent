"""Backfill dedup follows event UIDs through the real Board transport, not local row IDs."""
from contextlib import closing
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_block_notice import coalesce_superseded_blocks
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_ask_backfill import _legacy
from tests.hermes_cli.test_kanban_resolved_check_drop_notice import _assert_drop_wording, _drop
from tui_gateway import server


@pytest.mark.parametrize("kind", [kar.BROKEN_EVENT, kar.LOST_EVENT])
def test_transport_remaps_drop_ids_but_origin_gets_exactly_one_notice(tmp_path, monkeypatch, kind):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    source_path, receiver_path = home / "source.db", home / "kanban.db"
    svc = FakeService()
    with closing(kbc.connect(source_path)) as source, closing(kbc.connect(receiver_path)) as receiver:
        ksc.ensure_identity(source, "default", "source")
        ksc.ensure_identity(receiver, "default", "receiver")
        # Receiver-only history deliberately shifts every imported integer event ID.
        padding = kb.create_task(receiver, title="receiver history")
        with kb.write_txn(receiver):
            for _ in range(20):
                kb._append_event(receiver, padding, "commented", {})
        tid = kb.create_task(source, title="DNS", assignee="worker")
        _drop(source, tid, produce=False)
        _legacy(source, tid, kind)
        original = max(kb.list_events(source, tid), key=lambda e: e.id)
        assert original.uid
        kar.reconcile(source, now=9000)
        replay = max(kb.list_events(source, tid), key=lambda e: e.id)
        assert replay.payload == {"backfilled": True, "origin_event_uid": original.uid}
        ksc.push(source, svc.hub("source"), "default", "source", primary=False)
        ksc.pull(receiver, svc.hub("receiver"), "default", observers=True)
        drops = sorted((e for e in kb.list_events(receiver, tid) if e.kind == kind), key=lambda e: e.id)
        assert len(drops) == 2
        assert drops[0].id != original.id and drops[0].uid == original.uid
        assert drops[1].payload == replay.payload  # payload not rewritten by transport
        kbn.add_notify_sub(receiver, task_id=tid, platform="tui", chat_id="sync-drop")
        with kb.write_txn(receiver):
            receiver.execute("UPDATE kanban_notify_subs SET last_event_id=? WHERE task_id=?",
                             (drops[0].id - 1, tid))  # original drop is still unconsumed
        claims = []
        texts = server._collect_kanban_notifications({"session_key": "sync-drop"}, claims)
        assert len(texts) == len(claims) == 1, texts
        _assert_drop_wording(texts[0])
        claim = claims[0]
        assert kbn.ack_notify_claim(receiver, task_id=tid, platform="tui", chat_id="sync-drop", thread_id="",
                                    upto=claim["new"], from_cursor=claim["old"])
        assert server._collect_kanban_notifications({"session_key": "sync-drop"}) == []


def test_numeric_fallback_never_suppresses_synced_events_or_overrides_uid():
    old = SimpleNamespace(id=1, kind=kar.BROKEN_EVENT, uid="receiver-origin", payload={})
    replay = SimpleNamespace(id=2, kind=old.kind, uid="receiver-replay",
                             payload={"backfilled": True, "origin_event_id": 1})
    assert coalesce_superseded_blocks([old, replay]) == [old, replay]
    replay.payload["origin_event_uid"] = "different-origin"
    assert coalesce_superseded_blocks([old, replay]) == [old, replay]
    replay.payload["origin_event_uid"] = old.uid
    assert coalesce_superseded_blocks([old, replay]) == [replay]
    replay.kind = kar.LOST_EVENT
    assert coalesce_superseded_blocks([old, replay]) == [old, replay]


def _event_row(with_uid):
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE e (id INTEGER, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER"
              + (", uid TEXT)" if with_uid else ")"))
    c.execute("INSERT INTO e VALUES (1, 't', 'k', '{\"a\": 1}', 5" + (", 'u-1')" if with_uid else ")"))
    return c.execute("SELECT * FROM e").fetchone()


@pytest.mark.parametrize("with_uid,uid", [(True, "u-1"), (False, None)])
def test_event_from_row_uid_and_uidless(with_uid, uid):
    from hermes_cli.kanban_db_event import Event
    ev = kb.Event.from_row(_event_row(with_uid))
    assert (ev.uid, ev.payload, ev.run_id, ev.created_at) == (uid, {"a": 1}, None, 5)
    assert kb.Event is Event


@pytest.mark.parametrize("payload,origin_uid,suppressed", [
    ({"origin_event_uid": "u", "origin_event_id": 9}, "u", True),    # UID match suppresses
    ({"origin_event_uid": "u"}, "other", False),                      # UID mismatch keeps
    ({"origin_event_id": 1}, None, True),                             # UID-less local id suppresses
    ({"origin_event_uid": "", "origin_event_id": 1}, None, False),    # empty uid: no id fallback
    ({"origin_event_uid": 7, "origin_event_id": 1}, None, False),     # non-string uid: no id fallback
    ({"origin_event_id": 1}, "z", False),                             # origin has uid: local id ignored
    ({"origin_event_id": "bad"}, None, False),                        # malformed local id ignored
])
def test_origin_uid_guards(payload, origin_uid, suppressed):
    from hermes_cli.kanban_block_notice import _drop_backfilled_origins
    kind = kar.BROKEN_EVENT
    origin = SimpleNamespace(id=1, kind=kind, payload=None, uid=origin_uid)
    replay = SimpleNamespace(id=2, kind=kind, payload={"backfilled": True, **payload}, uid=None)
    assert [e.id for e in _drop_backfilled_origins([origin, replay])] == ([2] if suppressed else [1, 2])
