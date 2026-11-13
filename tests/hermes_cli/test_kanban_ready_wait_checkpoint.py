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
from hermes_cli import kanban_db_sync as sync
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


# --- a live board lagging its pulled replica (t_01a1d390d19239dd) -----------------------------------------------
# 2026-11-12 22:11 PST, MacBook: chat_wait_unclaimed for t_01a1d1292db9d3cb and t_01a1d12a594c8fd7, done on the Board
# 30 min earlier. The live feed cursor (1650734) trailed its own replica (1713384) after a sync-loop stall, so the
# live rows still read ready. These model that pair: a synced live board and a replica at sync-shadow/default.db.


@pytest.fixture
def synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import kanban_sync_client as ksc
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    support.write_sync_config(home, True)
    monkeypatch.setattr(cw, "_LAG_WARNED", set())
    c = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(c, "default", "macbook")
    sync.set_mode(c, "primary")  # the MacBook's mode: the live board applies the feed
    ksa._reset_cache_for_tests()
    yield c
    c.close()
    ksa._reset_cache_for_tests()


def _set_live(conn, tid, *, cursor, version):
    """The live board's feed cursor and the card's revision, with every local change pushed (outbox drained)."""
    conn.execute("UPDATE kanban_sync_state SET cursor = ?", (cursor,))
    conn.execute("UPDATE tasks SET sync_version = ? WHERE id = ?", (version, tid))
    conn.execute("DELETE FROM kanban_sync_outbox")
    conn.commit()


def _set_replica(*rows, cursor, pulled_at=None):
    """The pulled replica: its feed cursor, its ``mark_pulled`` stamp and ``(id, status, sync_version)`` rows."""
    import sqlite3
    from hermes_cli import kanban_sync_client as ksc
    path = ksc.shadow_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    rep = sqlite3.connect(path)
    rep.executescript(
        "CREATE TABLE IF NOT EXISTS kanban_sync_state (id INTEGER PRIMARY KEY CHECK (id = 1), cursor INTEGER, "
        "pulled_at REAL);"
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, ready_since INTEGER, "
        "sync_version INTEGER, result TEXT);"
        "CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, "
        "started_at INTEGER, ended_at INTEGER);")
    rep.execute("INSERT OR REPLACE INTO kanban_sync_state VALUES (1, ?, ?)",
                (cursor, time.time() if pulled_at is None else pulled_at))
    rep.executemany("INSERT OR REPLACE INTO tasks (id, status, sync_version) VALUES (?, ?, ?)", rows)
    rep.commit()
    rep.close()


def _waiting(conn):
    tid = kb.create_task(conn, title="waited on", assignee="default")
    _subscribe(conn, tid)
    _age(conn, tid)
    return tid


def test_replica_completed_later_skips_the_notice(synced, caplog):
    """(a) Live row ready 31 min; the replica, at the same feed position, has the card done at a newer revision."""
    tid = _waiting(synced)
    _set_live(synced, tid, cursor=1000, version=5)
    _set_replica((tid, "done", 7), cursor=1000)
    with caplog.at_level("INFO", logger="hermes_cli.kanban_chat_wait"):
        assert cw.sweep(synced) == 0
    assert _notices(synced, tid, cw.NOTICE_KIND) == []
    assert f"{tid} skipped: replica has it done (revision 7)" in caplog.text


def test_live_feed_lag_defers_the_notice_until_it_closes(synced, caplog):
    """(b) The replica also has the card ready, but the live feed trails it past the bound: nothing is said until
    the lag closes, then exactly one notice for the same episode."""
    from hermes_cli import kanban_replica_freshness as fresh
    tid = _waiting(synced)
    head = 1000 + fresh.MAX_FEED_LAG_ROWS + 1
    _set_live(synced, tid, cursor=1000, version=5)
    _set_replica((tid, "ready", 5), cursor=head)
    with caplog.at_level("INFO", logger="hermes_cli.kanban_chat_wait"):
        assert cw.sweep(synced) == 0
    assert _notices(synced, tid, cw.NOTICE_KIND) == []
    assert "1 notice(s) deferred: live feed" in caplog.text
    deferred = [r.levelname for r in caplog.records if "deferred" in r.getMessage()]
    assert deferred == ["WARNING"]  # the first deferred sweep of a stall warns: a long stall drops the episode
    with caplog.at_level("INFO", logger="hermes_cli.kanban_chat_wait"):
        assert cw.sweep(synced) == 0
    deferred = [r.levelname for r in caplog.records if "deferred" in r.getMessage()]
    assert deferred == ["WARNING", "INFO"]
    _set_live(synced, tid, cursor=head, version=5)
    assert cw.sweep(synced) == 1
    assert cw.sweep(synced) == 0
    assert len(_notices(synced, tid, cw.NOTICE_KIND)) == 1


def test_stale_replica_pull_defers_the_notice(synced):
    """A stalled sync loop stops both copies, so their cursors agree: the replica's pull stamp is what shows it."""
    from hermes_cli import kanban_replica_freshness as fresh
    tid = _waiting(synced)
    _set_live(synced, tid, cursor=1000, version=5)
    _set_replica((tid, "ready", 5), cursor=1000, pulled_at=time.time() - fresh.REPLICA_STALE_SECONDS - 60)
    assert cw.sweep(synced) == 0
    _set_replica((tid, "ready", 5), cursor=1000)
    assert cw.sweep(synced) == 1


def test_in_sync_replica_still_ready_posts_once(synced):
    """(c) Regression guard: no lag and the replica agrees the card is ready, so the notice goes out once."""
    tid = _waiting(synced)
    _set_live(synced, tid, cursor=1000, version=5)
    _set_replica((tid, "ready", 5), cursor=1000)
    assert cw.sweep(synced) == 1
    assert cw.sweep(synced) == 0
    assert len(_notices(synced, tid, cw.NOTICE_KIND)) == 1


def test_shadow_mode_board_is_not_compared(synced):
    """In ``shadow`` mode (``sync attach``'s default) the live board never applies the feed, so its cursor stays
    put while the replica's runs ahead: no lag defer and no cross-check, the live board's own notice goes out."""
    sync.set_mode(synced, "shadow")
    tid = _waiting(synced)
    _set_live(synced, tid, cursor=0, version=5)
    _set_replica((tid, "done", 7), cursor=50_000)
    assert cw.sweep(synced) == 1
    assert len(_notices(synced, tid, cw.NOTICE_KIND)) == 1


@pytest.mark.parametrize("how", ["detached", "disabled"])
def test_detached_or_disabled_board_is_not_compared(synced, tmp_path, how):
    """A board detached to ``off``, or one this home no longer syncs, keeps its replica on disk while pulls stop:
    its aging stamp must not hold the notice forever."""
    from hermes_cli import kanban_replica_freshness as fresh
    from tests.hermes_cli import kanban_sync_support as support
    tid = _waiting(synced)
    _set_live(synced, tid, cursor=1000, version=5)
    _set_replica((tid, "ready", 5), cursor=1000, pulled_at=time.time() - fresh.REPLICA_STALE_SECONDS - 86_400)
    if how == "detached":
        sync.set_mode(synced, "off")
        conn = synced
    else:  # a fresh process of a home whose config no longer syncs the board
        support.write_sync_config(tmp_path / ".hermes", False)
        kbc._INITIALIZED_PATHS.clear()
        sync._SYNCED_PATHS.clear()
        conn = kbc.connect(tmp_path / ".hermes" / "kanban.db")
    try:
        assert fresh.lag_hold(conn) is None
        assert cw.sweep(conn) == 1
    finally:
        if conn is not synced:
            conn.close()
    assert len(_notices(synced, tid, cw.NOTICE_KIND)) == 1


def _db_file(conn):
    return next(file for _seq, name, file in conn.execute("PRAGMA database_list") if name == "main")


def test_lagging_board_warns_once_per_stall_while_another_board_is_healthy(tmp_path, monkeypatch, caplog):
    """PR #1932 review nit: the dispatcher sweeps each board separately. Board A lags its replica, board B is
    healthy; B's undeferred sweep each tick must not re-arm A's WARNING, so A warns once per stall, not per tick."""
    from hermes_cli import kanban_replica_freshness as fresh
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cw, "_LAG_WARNED", set())
    a, b = kbc.connect(tmp_path / "a.db"), kbc.connect(tmp_path / "b.db")
    try:
        ta, tb = _waiting(a), _waiting(b)
        lagging = {_db_file(a)}
        monkeypatch.setattr(fresh, "lag_hold", lambda conn, now=None: (
            {"reason": "live feed 9999 rows behind its replica"} if _db_file(conn) in lagging else None))
        ticks = 5
        with caplog.at_level("INFO", logger="hermes_cli.kanban_chat_wait"):
            posted = [(cw.sweep(a), cw.sweep(b)) for _tick in range(ticks)]
        assert posted == [(0, 1)] + [(0, 0)] * (ticks - 1)  # B's card stays due, so every B sweep runs undeferred
        deferred = [r.levelname for r in caplog.records if "deferred" in r.getMessage()]
        assert deferred == ["WARNING"] + ["INFO"] * (ticks - 1)
        assert _notices(a, ta, cw.NOTICE_KIND) == [] and len(_notices(b, tb, cw.NOTICE_KIND)) == 1
        lagging.clear()  # A's lag closes: its own undeferred sweep ends the stall and posts the held notice
        assert cw.sweep(a) == 1
        lagging.add(_db_file(a))
        _waiting(a)  # a new due card on A, so the next stall has something to defer
        caplog.clear()
        with caplog.at_level("INFO", logger="hermes_cli.kanban_chat_wait"):
            cw.sweep(b)
            cw.sweep(a)
        deferred = [r.levelname for r in caplog.records if "deferred" in r.getMessage()]
        assert deferred == ["WARNING"]  # a new stall on A warns again
    finally:
        a.close()
        b.close()
