"""Busy Board replacement-key evidence: finite-cut coverage, not a reservation."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_key_evidence as evidence
from hermes_cli import kanban_sync_reserved_key as rk
from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable
from tests.hermes_cli import test_kanban_sync_reserved_key as base
from tests.hermes_cli.test_kanban_sync_reserved_key import BOARD, GOOD, ME, REASON, bad_card, dump

kanban_home, live, svc = base.kanban_home, base.live, base.svc


def change(seq, key=None, uid="a"):
    return {"seq": seq, "table": "tasks", "uid": uid, "op": "upsert",
            "fields": {"uid": uid, "idempotency_key": key}}


def feed(changes, head=None, more=False):
    end = changes[-1]["seq"] if changes else 10
    return {"changes": changes, "head": end if head is None else head, "next": end, "more": more}


def moving(hub, monkeypatch, changes):
    # The second read misses inserts/edits behind its cursor. Only the feed can see them.
    pages = {"": {"table": "tasks", "head": 10, "rows": [{"uid": "m", "idempotency_key": None}],
                  "next": "m", "more": True},
             "m": {"table": "tasks", "head": 12, "rows": [], "next": "m", "more": False}}
    monkeypatch.setattr(hub, "snapshot_page", lambda b, t, after="": pages[after])
    monkeypatch.setattr(hub, "changes", changes)


def test_busy_board_finite_cut_without_chasing_writers(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub, calls = svc.hub(ME), []

    def read(board, cursor):
        assert board == BOARD
        calls.append(cursor)
        # Writers are still ahead of the cut, and the feed is still paginated.
        return feed([change(cursor + 1)], head=100 + cursor, more=True)
    moving(hub, monkeypatch, read)
    before = dump(live)
    report = rk.plan(live, hub, BOARD, tid, GOOD, REASON)
    assert report["eligible"], report
    assert calls == [10, 11]
    assert dump(live) == before
    result = rk.apply(live, hub, BOARD, tid, GOOD, report["token"], REASON)
    assert result["action"] == "applied", result
    assert calls == [10, 11, 10, 11]  # apply refreshes, not a cached preview


@pytest.mark.parametrize("uid", ["a-new-behind-cursor", "m"])
def test_insert_or_edit_behind_cursor_vetoes(live, svc, monkeypatch, uid):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub = svc.hub(ME)
    moving(hub, monkeypatch, lambda *a: feed([change(11, GOOD, uid), change(12)]))
    before = dump(live)
    result = rk.apply(live, hub, BOARD, tid, GOOD, "old-preview", REASON)
    assert result["refusal"] == "replacement_key_in_remote_use"
    assert dump(live) == before


def test_transient_duplicate_then_delete_still_vetoes():
    changes = [change(11, GOOD), {"seq": 12, "table": "tasks", "uid": "a", "op": "delete", "fields": None}]
    hub = SimpleNamespace(changes=lambda *a: feed(changes))
    assert evidence.cover_changes(hub, BOARD, GOOD, 10, 12) == "replacement_key_in_remote_use"


@pytest.mark.parametrize("page", [
    None, {}, {"resync": True}, feed([]), feed([change(12)]),
    feed([change(11)], head=12), feed([change(11)], head=9),
    {**feed([change(11), change(12)]), "next": 13},
    {**feed([change(11), change(12)]), "more": "false"},
    feed([change(11), change(11)]),
    feed([{**change(11), "fields": {"uid": "a"}}, change(12)]),
    feed([{**change(11), "fields": None}, change(12)]),
    feed([{**change(11), "fields": {"uid": "other", "idempotency_key": None}}, change(12)]),
    feed([{**change(11), "op": "unknown"}, change(12)]),
])
def test_incomplete_or_malformed_feed_refuses(page):
    hub = SimpleNamespace(changes=lambda *a: page)
    assert evidence.cover_changes(hub, BOARD, GOOD, 10, 12).startswith("authoritative_snapshot_incomplete")


@pytest.mark.parametrize("exc", [HubError(404, "absent"), Resync("compacted"), SyncUnavailable("down"), OSError("network")])
def test_failed_or_compacted_feed_refuses(exc):
    def read(*a):
        raise exc
    assert evidence.cover_changes(SimpleNamespace(changes=read), BOARD, GOOD, 10, 12).startswith(
        "authoritative_snapshot_unavailable")


def test_missing_service_and_bounded_feed_refuse(monkeypatch):
    assert evidence.cover_changes(object(), BOARD, GOOD, 10, 12).startswith("authoritative_snapshot_unavailable")
    monkeypatch.setattr(evidence, "MAX_FEED_PAGES", 1)
    hub = SimpleNamespace(changes=lambda *a: feed([change(11)], head=12, more=True))
    assert "page cap" in evidence.cover_changes(hub, BOARD, GOOD, 10, 12)


@pytest.mark.parametrize("key", [None, GOOD])
def test_service_interleaving_behind_real_page_cursor(svc, monkeypatch, key):
    hub, b = svc.hub(ME), svc.board(BOARD)
    for uid in ("m", "z"):
        svc._put(b, "tasks", uid, {"idempotency_key": None}, ME, None, "")
    original = hub.snapshot_page
    calls = []

    def page(board, table, after=""):
        if after:
            # A different writer inserts below the page cursor after page one.
            svc._put(b, "tasks", "a", {"idempotency_key": key}, "other", None, "")
        result = original(board, table, after, limit=1)
        calls.append(result["head"])
        return result
    monkeypatch.setattr(hub, "snapshot_page", page)
    result = rk._remote_key_refusal(hub, BOARD, GOOD)
    assert calls == [2, 3]
    assert result == ("replacement_key_in_remote_use" if key == GOOD else None)


def test_feed_latency_counts_against_original_evidence_bound(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub, clock = svc.hub(ME), [0.0]
    monkeypatch.setattr(rk, "_mono", lambda: clock[0])

    def read(*a):
        clock[0] += rk.EVIDENCE_MAX_AGE_S + 1
        return feed([change(11), change(12)])
    moving(hub, monkeypatch, read)
    before = dump(live)
    result = rk.apply(live, hub, BOARD, tid, GOOD, "old-preview", REASON)
    assert result["refusal"] == "evidence_interval_exceeded"
    assert dump(live) == before
