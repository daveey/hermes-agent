"""Ready-wait notices name the card's last checkpoint, so a chat doesn't re-place or duplicate finished work. [daveey]

Defect kanban-3debc730ab (2026-11-12): t_01a1c59e0410b2fb merged PR #1148 at 08:15Z and recorded it only as the
checkpoint of its continued run ("LANDED: PR1148 MERGED ... as main 6948d8bc"); it then sat ready with no worker.
The ``chat_wait_unclaimed`` notice at 09:11Z carried queue state only, and the chat created a duplicate landing card
for the merged PR. These tests pin: the checkpoint in both ready-wait notices and the ``kanban_show`` ready hint,
no sentence for a card with no parked run, and unchanged text for an old payload without the field.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_parent_unclaimed as pu

LANDED = "LANDED: PR1148 MERGED 08:15:20Z as main 6948d8bc; NEXT: wait for main CI then live-check"
SENTENCE = "Read it before re-placing the card or creating a replacement; resume or finish what it records."


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


def _subscribe(conn, tid):
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "create"})


def _continued(conn, title="land PR #1148"):
    """A ready card whose latest run parked with ``kanban_continue`` (a two-line checkpoint)."""
    tid = kb.create_task(conn, title=title, assignee="default")
    assert kb.claim_task(conn, tid)
    assert kc.continue_task(conn, tid, checkpoint=LANDED + "\nState: main CI run pending")["ok"]
    assert kb.get_task(conn, tid).status == "ready"
    return tid


def _age(conn, *tids, seconds=31 * 60):
    then = int(time.time()) - seconds
    for tid in tids:
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (then, tid))
        conn.execute("UPDATE task_runs SET started_at=?, ended_at=? WHERE task_id=?", (then - 60, then, tid))
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (then, tid))
    conn.commit()
    return then


def _notices(conn, tid, kind):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]


def test_chat_wait_notice_names_the_continued_runs_checkpoint(conn):
    tid = _continued(conn)
    _subscribe(conn, tid)
    then = _age(conn, tid)
    assert cw.sweep(conn) == 1
    [payload] = _notices(conn, tid, cw.NOTICE_KIND)
    assert payload["last_checkpoint"] == LANDED  # first line only
    assert payload["last_checkpoint_at"] == then
    text = cw.notice_text(payload)
    stamp = time.strftime("%H:%MZ", time.gmtime(then))
    assert f'Last checkpoint ({stamp}): "{LANDED}". {SENTENCE}' in text
    assert "State: main CI run pending" not in text
    # It comes before the advice that points the chat at re-placement.
    assert text.index("Last checkpoint") < text.index("can't start is no fix")


def test_parent_unclaimed_notice_names_the_parents_checkpoint(conn):
    parent = _continued(conn)
    child = kb.create_task(conn, title="live-check #1148", assignee="default", parents=[parent])
    _subscribe(conn, child)
    _age(conn, parent, child)
    assert pu.sweep(conn) == 1
    [payload] = _notices(conn, child, pu.NOTICE_KIND)
    text = pu.notice_text(payload)
    assert "Last checkpoint" in text and LANDED in text and SENTENCE in text
    assert text.index("Last checkpoint") < text.index("can't start is no fix")


def test_kanban_show_ready_hint_names_the_checkpoint(conn, monkeypatch):
    from tools import kanban_show
    tid = _continued(conn)
    _age(conn, tid)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    hint = json.loads(kanban_show.show({"task_id": tid})).get("queue_hint") or ""
    assert "Last checkpoint" in hint and LANDED in hint and SENTENCE in hint
    assert "can't start is no fix" in hint


def test_card_without_a_prior_run_gets_no_checkpoint(conn, monkeypatch):
    from tools import kanban_show
    tid = kb.create_task(conn, title="never ran", assignee="default")
    _subscribe(conn, tid)
    _age(conn, tid)
    assert cw.sweep(conn) == 1
    [payload] = _notices(conn, tid, cw.NOTICE_KIND)
    assert "last_checkpoint" not in payload
    assert "Last checkpoint" not in cw.notice_text(payload)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert "Last checkpoint" not in (json.loads(kanban_show.show({"task_id": tid})).get("queue_hint") or "")


@pytest.mark.parametrize("kind", [cw.NOTICE_KIND, pu.NOTICE_KIND])
@pytest.mark.parametrize("state", ["included", "no_run", "nonpark", "unfinished", "empty_summary", "lookup_error"])
def test_notice_records_selection_and_emitter_without_summary(conn, monkeypatch, kind, state):
    from hermes_cli import kanban_core_update_requeue as requeue
    from hermes_cli import git_fingerprint
    from hermes_cli import kanban_sync_client as sync
    from types import SimpleNamespace
    import os

    tid = _continued(conn)
    run = conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)).fetchone()
    if state == "no_run":
        monkeypatch.setattr(requeue, "latest_run_skipping_requeues", lambda *_: None)
    elif state == "lookup_error":
        def fail(*_):
            raise OSError("private detail must not reach event")
        monkeypatch.setattr(requeue, "latest_run_skipping_requeues", fail)
    elif state != "included":
        updates = {"nonpark": ("completed", run["ended_at"], LANDED),
                   "unfinished": ("continued", None, LANDED),
                   "empty_summary": ("continued", run["ended_at"], "")}
        conn.execute("UPDATE task_runs SET outcome=?,ended_at=?,summary=? WHERE id=?", (*updates[state], run["id"]))
        conn.commit()
    monkeypatch.setattr(sync, "load_identity", lambda: SimpleNamespace(gateway="emitting-worker"))
    monkeypatch.setattr(git_fingerprint, "process_boot_fingerprint", lambda _: "git:HEAD:" + "a" * 40)
    assert pu.post_notice(conn, tid, tid, wait_key="diagnostic", waited_seconds=1900,
                          ready_seconds=1900, kind=kind)
    [payload] = _notices(conn, tid, kind)
    diagnostic = payload["checkpoint_diagnostic"]
    assert diagnostic["reason"] == state
    assert diagnostic["emitter_gateway"] == "emitting-worker"
    assert diagnostic["emitter_pid"] == os.getpid()
    assert diagnostic["core_revision"] == "a" * 40
    if state not in {"no_run", "lookup_error"}:
        assert diagnostic["run_id"] == run["id"]
        assert diagnostic["outcome"] == ("completed" if state == "nonpark" else "continued")
        assert diagnostic["ended_at"] == (None if state == "unfinished" else run["ended_at"])
    assert ("last_checkpoint" in payload) == (state == "included")
    assert LANDED not in json.dumps(diagnostic)
    assert "private detail" not in json.dumps(diagnostic)
    assert not pu.post_notice(conn, tid, tid, wait_key="diagnostic", waited_seconds=1900,
                              ready_seconds=1900, kind=kind)


@pytest.mark.parametrize("guard", [None, "active_pr"])
def test_sweep_records_checkpoint_before_or_after_guard_observation(conn, monkeypatch, guard):
    tid = _continued(conn)
    _subscribe(conn, tid)
    _age(conn, tid)
    monkeypatch.setattr(pu, "_place", lambda *_: {
        "lane": "respawn_guard" if guard else "skipped_ineligible", "reason": guard,
        "board": "default", "observed_at": int(time.time()),
    })
    assert cw.sweep(conn) == 1
    [payload] = _notices(conn, tid, cw.NOTICE_KIND)
    assert payload["checkpoint_diagnostic"]["reason"] == "included"
    assert payload["last_checkpoint"] == LANDED
    assert payload["respawn_guard"] == guard
    assert cw.sweep(conn) == 0


def test_unknown_boot_and_unenrolled_emitter_still_posts(conn, monkeypatch):
    from hermes_cli import git_fingerprint
    from hermes_cli import kanban_sync_client as sync
    monkeypatch.setattr(git_fingerprint, "process_boot_fingerprint", lambda _: None)
    def unavailable():
        raise sync.SyncUnavailable("private path")
    monkeypatch.setattr(sync, "load_identity", unavailable)
    tid = _continued(conn)
    _subscribe(conn, tid)
    _age(conn, tid)
    assert cw.sweep(conn) == 1
    [payload] = _notices(conn, tid, cw.NOTICE_KIND)
    diagnostic = payload["checkpoint_diagnostic"]
    assert diagnostic["core_revision"] is None
    assert diagnostic["emitter_gateway"] is None
    assert diagnostic["emitter_host"]
    assert diagnostic["reason"] == "included"
    assert "private path" not in json.dumps(diagnostic)


@pytest.mark.parametrize("render", [cw.notice_text, pu.notice_text])
def test_old_payload_without_the_field_renders_unchanged(render):
    old = {"parent": "t_old", "parent_title": "old card", "wait_minutes": 40, "ready_minutes": 35,
           "queue": "2 card(s) ahead of it"}
    before = render(old)
    assert "Last checkpoint" not in before
    with_field = render({**old, "last_checkpoint": LANDED, "last_checkpoint_at": 1794470400})
    sentence = f'Last checkpoint (08:00Z): "{LANDED}". {SENTENCE} '
    assert sentence in with_field
    assert with_field.replace(sentence, "") == before
