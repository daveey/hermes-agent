"""Follow-ups to the push-before-pull round (PR #1934 reviews, t_01a1d41c2612ebc0).

1. A transport failure on the push before the pulls never stops the pulls.
2. The outbox-age alarm ages only what the push before the head would send (the FIFO cut at the first pending run
   row, and a stale wake held for a card the Board completed), so a long catch-up never alarms on held ops.
3. ``kanban_worker_repin`` pulls the live board only under ``board_sync_lock``, and confirms its CAS by a targeted
   read when a backlog stops the bounded read-back short of the repinned row.
4. ``sync diff`` neither rebuilds the replica nor transfers attachments.
5. One Board read per card per round, shared by both sends.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_outbox_age as outbox_age
from hermes_cli import kanban_worker_placement as wp
from hermes_cli.kanban_sync_guard import board_sync_lock
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_sync_push_starvation as starvation
from tests.hermes_cli import test_kanban_worker_repin as repin_fixtures
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_sync_push_starvation import (BACKLOG_PAGES, BOARD, ME, OTHER, PagedHub,
                                                               _other_creates, _round)
from tests.hermes_cli.test_kanban_worker_node import HUB

kanban_home, svc, live = starvation.kanban_home, starvation.svc, starvation.live
gws = repin_fixtures.gws


@pytest.fixture(autouse=True)
def _fresh_loop_state():
    backlog.reset()
    outbox_age.reset()
    yield
    backlog.reset()
    outbox_age.reset()


def _replica_tasks():
    with kbc.connect_closing(ksc.shadow_path(BOARD)) as shadow:
        return {r[0] for r in shadow.execute("SELECT id FROM tasks")}


# --- 1. a failed push before the pulls does not stop them ----------------------------------------------------------

class OpsDown(PagedHub):
    """The Board's /ops answers 503 (``fail`` times, or always) while /changes keeps working."""

    def __init__(self, svc_, gateway, log, fail=None):
        super().__init__(svc_, gateway, log)
        self.fail = fail

    def ops(self, board, ops):
        if self.fail is None or self.fail > 0:
            self.fail = None if self.fail is None else self.fail - 1
            self.log.append(("ops-503", [op["uid"] for op in ops]))
            raise ksc.HubError(503, "service unavailable")
        return super().ops(board, ops)


def test_an_ops_outage_still_pulls_every_remote_row_and_the_round_reports_the_push_error(live, svc, kanban_home):
    assert _round(kanban_home, svc.hub(ME)).error is None
    _other_creates(svc, 3)
    kb.create_task(live, title="local edit")
    log: list = []
    with pytest.raises(ksc.HubError):
        _round(kanban_home, OpsDown(svc, ME, log))
    assert {f"t_other{i:03d}" for i in range(3)} <= _replica_tasks()  # RED before: 0 of 3, no /changes call
    assert any(kind == "changes" for kind, _ in log)
    results = ksc.sync_all_once(OpsDown(svc, ME, []), boards=[(BOARD, kanban_home / "kanban.db")])
    assert results[0].error and "HubError" in results[0].error


def test_a_behind_round_whose_send_after_the_pulls_fails_still_reruns_at_once(live, svc, kanban_home):
    """The pulls landed before the send after them raised: a board still behind is noted for an immediate rerun
    (``backlog.due``) before the error is re-raised, so an /ops outage never slows the catch-up to the idle
    interval."""
    path = kanban_home / "kanban.db"
    assert _round(kanban_home, svc.hub(ME)).error is None
    _other_creates(svc, BACKLOG_PAGES)
    kb.create_task(live, title="local edit")
    log: list = []
    with pytest.raises(ksc.HubError):
        _round(kanban_home, OpsDown(svc, ME, log))
    assert [kind for kind, _ in log if kind == "ops-503"] == ["ops-503", "ops-503"]  # both sends were tried
    assert backlog.behind(path)  # RED with the note dropped: the round's opening note(False) stands
    assert backlog.due([(BOARD, path)]) == [(BOARD, path)]


def test_a_push_that_fails_once_before_the_pulls_is_sent_by_the_push_after_them(live, svc, kanban_home):
    tid = kb.create_task(live, title="local edit")
    _other_creates(svc, 3)
    log: list = []
    result = _round(kanban_home, OpsDown(svc, ME, log, fail=1))
    assert result.error is None and log[0][0] == "ops-503"
    assert svc.board(BOARD)["rows"]["tasks"][tid]["title"] == "local edit"
    assert {f"t_other{i:03d}" for i in range(3)} <= _replica_tasks()


# --- 2. the outbox-age alarm under the before-head holds ------------------------------------------------------------

def _age_pending(c, minutes):
    with kbc.write_txn(c):  # one timestamp for every pending row: FIFO order is kept by rowid
        c.execute("UPDATE kanban_sync_outbox SET created_at = ? WHERE state = 'pending'",
                  (int((time.time() - minutes * 60) * 1000),))


class _Alarm:
    """Both alarm readers after each round: the sync loop's WARNING and the dispatcher tick's card."""

    def __init__(self, path):
        self.boards, self.clock, self.filed = [(BOARD, path)], [0.0], []

    def after(self, result):
        warned = outbox_age.after_round([result], self.boards)
        self.clock[0] += outbox_age.CHECK_EVERY_S
        filed = outbox_age.check(mono=lambda: self.clock[0], file_fn=lambda s, d: self.filed.append(d) or "t_x",
                                 boards=self.boards)
        return warned + filed


def test_a_catch_up_never_alarms_on_ops_held_behind_a_pending_run_row(kanban_home, caplog):
    """A pending run start, then a later task edit, both older than STALE_S, over a multi-round catch-up: the push
    before the head holds both (FIFO cut), so no WARNING and no card; at the head both go and nothing stays stale."""
    from tests.hermes_cli import test_kanban_sync_run_rows as run_rows
    svc_ = FakeService(lease_runs=True)
    support.write_sync_config(kanban_home, True)
    c = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(c, BOARD, ME)
    sync.set_mode(c, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc_.hub(ME)))
    path = kanban_home / "kanban.db"
    try:
        _tid, uid = run_rows._claimed(c, kanban_home, svc_, "claimed while asleep")
        later = kb.create_task(c, title="edited after the claim")
        rows = [r[0] for r in c.execute("SELECT table_name FROM kanban_sync_outbox WHERE state = 'pending' "
                                        "ORDER BY created_at, rowid")]
        assert "task_runs" in rows and rows.index("task_runs") < len(rows) - 1, rows
        _other_creates(svc_, BACKLOG_PAGES)
        _age_pending(c, minutes=30)
        alarm = _Alarm(path)
        hub = PagedHub(svc_, ME, [])
        rounds = 0
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_outbox_age"):
            while (result := _round(kanban_home, hub)).behind:
                rounds += 1
                assert result.error is None
                assert alarm.after(result) == [], f"round {rounds}: alarmed on held ops"
            assert rounds >= 2  # a multi-round catch-up
            assert alarm.after(result) == []
        assert not [r for r in caplog.records if "waited" in r.getMessage()] and alarm.filed == []
        assert c.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0] == 0
        assert outbox_age._age(path) is None
        assert svc_.board(BOARD)["rows"]["tasks"][later]["title"] == "edited after the claim"
        assert uid in svc_.board(BOARD)["rows"]["task_runs"]
    finally:
        sync.set_lease_client(previous)
        c.close()


def _stale_wakes(live_, svc_, kanban_home_, n):
    """``n`` cards the Board completed in a page the next round does not reach, each with a local wake pending."""
    cards = [kb.create_task(live_, title=f"done elsewhere {i}", assignee="worker") for i in range(n)]
    with kbc.write_txn(live_):
        live_.execute(f"UPDATE tasks SET status = 'scheduled' WHERE id IN ({','.join('?' * n)})", cards)
    assert _round(kanban_home_, svc_.hub(ME)).error is None
    _other_creates(svc_, BACKLOG_PAGES)
    now = time.time_ns() // 1_000_000
    ops = []
    for i, tid in enumerate(cards):
        ops += [{"client_op_id": f"done-{tid}", "op": "upsert", "table": "tasks", "uid": tid,
                 "hlc": f"{now + 2 * i:013d}-0000-{OTHER}",
                 "fields": {"status": "done", "completed_at": int(time.time())}},
                {"client_op_id": f"ev-{tid}", "op": "upsert", "table": "task_events", "uid": sync.uuid7(),
                 "hlc": f"{now + 2 * i + 1:013d}-0000-{OTHER}",
                 "fields": {"task_id": tid, "kind": "completed", "payload": "{}", "created_at": int(time.time())}}]
    svc_.hub(OTHER).ops(BOARD, ops)
    with kbc.write_txn(live_):
        live_.execute(f"UPDATE tasks SET status = 'ready' WHERE id IN ({','.join('?' * n)})", cards)
    return cards


def test_a_held_stale_wake_never_alarms_during_the_catch_up(live, svc, kanban_home, caplog):
    path = kanban_home / "kanban.db"
    [stale] = _stale_wakes(live, svc, kanban_home, 1)
    alarm = _Alarm(path)
    hub = PagedHub(svc, ME, [])
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_outbox_age"):
        result = _round(kanban_home, hub)
        assert result.behind
        _age_pending(live, minutes=30)  # the held wake is now far older than STALE_S
        assert alarm.after(result) == []
        while (result := _round(kanban_home, hub)).behind:
            _age_pending(live, minutes=30)
            assert alarm.after(result) == []
        assert alarm.after(result) == []
    assert not [r for r in caplog.records if "waited" in r.getMessage()] and alarm.filed == []
    assert svc.board(BOARD)["rows"]["tasks"][stale]["status"] == "done"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ? "
                            "AND state = 'pending'", (stale,)).fetchone()  # stripped and sent at the head
    assert (outbox_age._age(path) or 0.0) < outbox_age.STALE_S  # only this round's own fresh bookkeeping, if any


def test_a_sendable_op_still_alarms_while_behind(live, kanban_home):
    """The hold is exact, not a blanket: an op the push before the head would send ages the alarm while behind."""
    path = kanban_home / "kanban.db"
    kb.create_task(live, title="sendable but stuck")
    _age_pending(live, minutes=30)
    backlog.note(path, True)
    assert outbox_age._age(path) == pytest.approx(30 * 60, abs=5)


def test_a_board_deferred_op_never_ages_the_alarm_while_behind(live, kanban_home):
    """An op the Board deferred (``retry_after`` set, now in the past, so the push before the head selects it) is a
    Board-side wait: only unanswered ops (``retry_after IS NULL``) age the alarm while behind."""
    path = kanban_home / "kanban.db"
    kb.create_task(live, title="deferred by the Board")
    _age_pending(live, minutes=30)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET retry_after = ? WHERE state = 'pending'", (int(time.time()) - 60,))
    backlog.note(path, True)
    with kbc.connect_closing(path) as conn:
        assert outbox_age.sendable_age_s(conn, path) is None  # RED with every sendable row aged: 1800 s
    assert outbox_age._age(path) is None
    kb.create_task(live, title="unanswered")
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET created_at = ? WHERE state = 'pending' AND retry_after IS NULL",
                     (int((time.time() - 120) * 1000),))
    assert outbox_age._age(path) == pytest.approx(120, abs=5)  # the unanswered op's age, not the deferred one's


# --- 5. one Board read per card per round ---------------------------------------------------------------------------

class CountingHub(PagedHub):
    def __init__(self, svc_, gateway, log):
        super().__init__(svc_, gateway, log)
        self.reads: list = []

    def row_evidence(self, board, table, uid):
        self.reads.append(uid)
        return self._inner.row_evidence(board, table, uid)


def test_a_behind_round_reads_each_held_card_from_the_board_once(live, svc, kanban_home):
    cards = _stale_wakes(live, svc, kanban_home, 3)
    hub = CountingHub(svc, ME, [])
    assert _round(kanban_home, hub).behind
    assert sorted(hub.reads) == sorted(cards)  # RED before: 6, the send after the pulls read them again
    assert backlog.evidence(kanban_home / "kanban.db") == {tid: True for tid in cards}


def test_the_send_after_the_pulls_at_the_head_reads_nothing(live, svc, kanban_home, monkeypatch):
    """The push before the pulls must read (only its pull tells an at-head round); the one after them at the
    head never does, so an at-head round reads each card at most once."""
    stale = kb.create_task(live, title="t", assignee="worker")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (stale,))
    assert _round(kanban_home, svc.hub(ME)).error is None
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (stale,))
    hub = CountingHub(svc, ME, [])
    sends: list = []
    real_send = ksc._send
    monkeypatch.setattr(ksc, "_send", lambda *a, **kw: sends.append((kw["after_pull"], len(hub.reads)))
                        or real_send(*a, **kw))
    assert not _round(kanban_home, hub).behind
    assert hub.reads == [stale] and sends == [(False, 0), (True, 1)]


# --- 3. repin: one live puller per board, and a capped read-back still confirms --------------------------------------

def _cursor(conn_):
    return int((sync.sync_state(conn_) or {}).get("cursor") or 0)


def _foreign_changes(svc_, n):
    b = svc_.board(BOARD)
    for i in range(n):
        svc_._put(b, "tasks", f"t_foreign{i:03d}", {"title": f"f{i}", "status": "ready", "created_on": "worker-1",
                                                     "created_at": 1},
                  "worker-1", None, f"{1_000 + i:013d}-0000-worker-1")


def test_repin_never_pulls_the_live_board_while_a_round_holds_it(gws, svc):
    origin, _ = gws
    tid, _ = repin_fixtures._policy_card(origin)
    _foreign_changes(svc, 3)
    before = _cursor(origin.conn)
    with board_sync_lock(Path(sync._db_key(origin.conn))):
        res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x",
                             hub=repin_fixtures.RepinHub(svc, HUB))
        assert _cursor(origin.conn) == before  # RED before: the read-back pulled under the round
    assert res["ok"] and res["readback"] == "pending" and "sync round holds" in res["note"], res
    assert svc.board(BOARD)["rows"]["tasks"][tid]["placement_policy"] is None
    origin.round()
    assert repin_fixtures._local(origin.conn, tid) == (None, ["machine:hub"], "ready")


class PagedRepinHub(repin_fixtures.RepinHub):
    """One feed change per page, so a few dozen Board writes outrun the read-back's page budget."""

    def changes(self, board, since, limit=500):
        return super().changes(board, since, limit=1)


def test_repin_behind_a_backlog_longer_than_the_page_budget_still_confirms_its_cas(gws, svc):
    origin, _ = gws
    tid, _ = repin_fixtures._policy_card(origin)
    _foreign_changes(svc, backlog.PULL_PAGE_BUDGET + 10)
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=PagedRepinHub(svc, HUB))
    assert res["ok"] and res["readback"] == "confirmed", res  # RED before: pending, the row was past page 20
    assert repin_fixtures._local(origin.conn, tid) == (None, ["machine:hub"], "ready")
    assert _cursor(origin.conn) < svc.board(BOARD)["seq"]  # the cursor kept its bounded pull, not the targeted read


def test_repin_conflict_behind_a_backlog_reads_the_newest_row_and_cas_again(gws, svc):
    origin, _ = gws
    tid, _ = repin_fixtures._policy_card(origin)
    _foreign_changes(svc, backlog.PULL_PAGE_BUDGET + 10)
    b = svc.board(BOARD)
    svc._put(b, "tasks", tid, {"priority": 3}, "worker-1", None, f"{9_999_999_999_996:013d}-0000-x")  # unseen edit
    hub = PagedRepinHub(svc, HUB)
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=hub)
    assert res.get("ok") is True and len(hub.repins) == 2, res
    assert hub.repins[1]["expected_revision"] > hub.repins[0]["expected_revision"]


def test_repin_conflict_while_a_round_holds_the_board_does_not_cas_blind(gws, svc):
    origin, _ = gws
    tid, _ = repin_fixtures._policy_card(origin)
    b = svc.board(BOARD)
    svc._put(b, "tasks", tid, {"priority": 3}, "worker-1", None, f"{9_999_999_999_996:013d}-0000-x")  # unseen edit
    hub = repin_fixtures.RepinHub(svc, HUB)
    with board_sync_lock(Path(sync._db_key(origin.conn))):
        res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=hub)
    assert res["reason_code"] == "revision_conflict" and len(hub.repins) == 1, res
    assert b["rows"]["tasks"][tid]["placement_policy"] == wp.POLICY


# --- 4. sync diff is a diagnostic ------------------------------------------------------------------------------------

def _diff(svc_, kanban_home_, monkeypatch, capsys, hub):
    from hermes_cli import kanban_sync_cli
    monkeypatch.setattr(ksc, "load_identity", lambda: svc_.hub(ME).ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: hub)
    code = kanban_sync_cli._diff(argparse.Namespace(board=BOARD, json=True))
    return code, json.loads(capsys.readouterr().out)


def test_sync_diff_never_transfers_attachments(live, svc, kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_sync_attachments
    assert _round(kanban_home, svc.hub(ME)).error is None
    transfers: list = []
    monkeypatch.setattr(kanban_sync_attachments, "transfer_pending", lambda *a, **kw: transfers.append(a))
    _other_creates(svc, 2)
    _code, report = _diff(svc, kanban_home, monkeypatch, capsys, svc.hub(ME))
    assert transfers == [] and not report.get("unavailable"), report
    _round(kanban_home, svc.hub(ME))
    assert transfers  # the sync round still transfers


def test_sync_diff_reports_a_resync_instead_of_rebuilding_the_replica(live, svc, kanban_home, monkeypatch, capsys):
    assert _round(kanban_home, svc.hub(ME)).error is None
    replica = ksc.shadow_path(BOARD)
    inode = replica.stat().st_ino
    hub = svc.hub(ME)

    def changes(board, since, limit=500):
        raise ksc.Resync("cursor below the retention floor")
    hub.changes = changes
    code, report = _diff(svc, kanban_home, monkeypatch, capsys, hub)
    assert code == 1 and report["unavailable"] and "retention floor" in report["refresh_error"], report
    assert replica.stat().st_ino == inode  # RED before: unlinked and rebuilt from a snapshot
