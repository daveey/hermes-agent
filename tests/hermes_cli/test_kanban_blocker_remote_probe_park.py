"""A park whose wake_check runs on another gateway is an independent wait: a wall clear must not wake it. [daveey]

Repro of 2026-11-15 PST on the MacBook board (the cards' home), wall ``github-daveey-core-reserve``:

- 02:40:41 t_01a1de1a97ec6274 (worker-7) and 03:06:43 t_01a1de371fb68f3d (worker-8) were behind the wall from an
  earlier run. Each ran again on a pool worker, published its core PR and re-parked on its own exact-head CI probe
  (``hermes-core-wt-land --review-ci-probe``) with no ``blocker_id``. The park ran on the worker, so it stamped
  ``probe_host`` = that worker, and the home registered no ``task_wake_checks`` row
  (``kanban_wake_check.set_check(register_here=False)``).
- 02:40:44 and 03:07:10: the home's registered check cleared the wall, and ``release_cleared_blockers`` woke both
  cards out of their CI waits (``shared_blocker_cleared`` with ``woke: true``). The ready cards could only re-park.

``own_wait`` (c18824850e) keeps an independent park only when ``kwc.get_state`` finds the probe row on this board,
and a park probed on another gateway never has one here. Not caught because every independent-park test parks on
the board that runs the probe.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_continuation import pending_wake
from tests.hermes_cli import test_kanban_blocker_park_wake as pw
import tools.kanban_tools as kt

kanban_home = pw.kanban_home
BID = pw.BID
HOME, WORKER = "macbook", "worker-8"
CI_PROBE = ("hermes-core-wt-land --review-ci-probe --repo softmax-universe/daveey-hermes --number 3308 "
            "--sha 099983f370fbb2eb0f96df43596dd5b66755a8bd --check 'All required checks pass (v2)'")
CI_AWAIT = {"kind": "ci", "ref": "https://github.com/softmax-universe/daveey-hermes/pull/3308",
            "why": "required checks on the exact head are still running"}


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """This board syncs; ``gateway[0]`` is the gateway the code runs on (the home unless a test moves it)."""
    host = [HOME]
    monkeypatch.setattr(kwc, "own_gateway", lambda conn=None: host[0])
    return host


def _two_behind(monkeypatch) -> tuple[str, str]:
    with kbc.connect_closing() as conn:
        a = pw._worker_block_behind(monkeypatch, conn, "lander")
        b = pw._worker_block_behind(monkeypatch, conn, "other card behind the wall")
        pw._age_all(conn, 600)
    return a, b


def _run_on_worker_then_park(monkeypatch, gateway, tid: str, park: dict) -> None:
    """``tid`` starts a run (before the clear), and that run parks on a pool worker; the home sees the synced park."""
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        pw._as_worker(monkeypatch, conn, tid)
    gateway[0] = WORKER
    try:
        out = json.loads(kt._handle_schedule(dict(park)))
    finally:
        gateway[0] = HOME
        pw._as_parent(monkeypatch)
    assert "error" not in out, out
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        # The home applies the worker's park without registering its probe (set_check register_here=False).
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))


def _clear_by_registered_check(conn) -> None:
    assert ksb.blocker_states(conn)[BID].is_open
    assert checks.run_due_checks(conn, cfg={"kanban": {"shared_blocker_checks": {BID: "exit 0"}}}) == [BID]
    st = ksb.blocker_states(conn)[BID]
    assert not st.is_open and st.cleared_via == "check"


def _woke(conn, tid: str) -> list:
    return [e for e in pw._cleared_events(conn, tid) if e.payload.get("woke")]


def test_remote_probe_park_keeps_its_wait_when_a_registered_check_clears_the_wall(kanban_home, monkeypatch, gateway):
    """Acceptance (a)."""
    a, b = _two_behind(monkeypatch)
    _run_on_worker_then_park(monkeypatch, gateway, a, {
        "reason": "review deferred: required checks pending on the exact head",
        "wake_check": CI_PROBE, "check_every": 120, "awaiting": CI_AWAIT})
    with kbc.connect_closing() as conn:
        park = pending_wake(conn, a)
        assert park and park.get("probe_host") == WORKER and park.get("wake_check") == CI_PROBE
        assert "blocker_id" not in park
        assert kwc.get_state(conn, a) is None          # no probe row on the home: the worker runs it
        _clear_by_registered_check(conn)
        released = ksb.release_cleared_blockers(conn)
        assert a not in released and b in released
        assert kb.get_task(conn, a).status == "scheduled"
        assert pending_wake(conn, a) == park           # its CI wait, backstop and probe custody intact
        assert not _woke(conn, a)
        assert ksb.release_cleared_blockers(conn) == []


def test_bare_clock_repark_from_a_worker_is_still_woken_by_the_clear(kanban_home, monkeypatch, gateway):
    """Acceptance (b): a re-park with no wake_check of its own still wakes when the wall clears (pinned)."""
    a, b = _two_behind(monkeypatch)
    _run_on_worker_then_park(monkeypatch, gateway, a, {
        "reason": f"SCHEDULED_UNTIL={pw._tomorrow()} wall still up; re-check tomorrow", "clock_only": True})
    with kbc.connect_closing() as conn:
        park = pending_wake(conn, a)
        assert park and "wake_check" not in park and "probe_host" not in park
        _clear_by_registered_check(conn)
        released = ksb.release_cleared_blockers(conn)
        assert a in released and b in released
        assert kb.get_task(conn, a).status == "ready"
        assert len(_woke(conn, a)) == 1


def test_local_probe_park_from_the_home_is_unchanged(kanban_home, monkeypatch, gateway):
    """The c18824850e case on the probing board itself: kept before and after this change."""
    a, b = _two_behind(monkeypatch)
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (a,))
        conn.commit()
        pw._as_worker(monkeypatch, conn, a)
    out = json.loads(kt._handle_schedule({"reason": "review deferred: required checks pending",
                                          "wake_check": CI_PROBE, "check_every": 120, "awaiting": CI_AWAIT}))
    pw._as_parent(monkeypatch)
    assert "error" not in out, out
    with kbc.connect_closing() as conn:
        assert kwc.get_state(conn, a)["cmd"] == CI_PROBE
        _clear_by_registered_check(conn)
        released = ksb.release_cleared_blockers(conn)
        assert a not in released and b in released
        assert kb.get_task(conn, a).status == "scheduled" and not _woke(conn, a)


def _own_probe_fires(monkeypatch, gateway, where: str, *, awaits_wall: bool = False) -> tuple[str, str]:
    """a (behind X) runs, parks on its own CI probe on ``where``, and that probe exits 0 while X is still open."""
    a, b = _two_behind(monkeypatch)
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (a,))
        conn.commit()
        pw._as_worker(monkeypatch, conn, a)
    gateway[0] = where
    try:
        out = json.loads(kt._handle_schedule({"reason": "review deferred: required checks pending",
                                              "wake_check": CI_PROBE, "check_every": 120, "awaiting": CI_AWAIT}))
        assert "error" not in out, out
        pw._as_parent(monkeypatch)
        with kbc.connect_closing() as conn:
            if awaits_wall:   # the park waits on the wall itself (awaiting.shared_blocker), not on its own work
                with kb.write_txn(conn):
                    park = pending_wake(conn, a)
                    assert park is not None
                    park["awaiting"]["shared_blocker"] = BID
                    kb._append_event(conn, a, "scheduled", park)
            state = kwc.get_state(conn, a)
            assert kwc.apply_result(conn, a, state["cmd"], 0, "All required checks pass (v2): success",
                                    generation=state["generation"]) == kwc.CONDITION_MET_EVENT
    finally:
        gateway[0] = HOME
    return a, b


def _status(conn, tid: str) -> str:
    task = kb.get_task(conn, tid)
    assert task is not None
    return task.status


@pytest.mark.parametrize("where", [HOME, WORKER])
def test_own_probe_firing_while_the_wall_is_open_is_not_held_on_it(kanban_home, monkeypatch, gateway, where):
    """Acceptance (c): the card's own wake_check exits 0 while X is still open."""
    a, b = _own_probe_fires(monkeypatch, gateway, where)
    with kbc.connect_closing() as conn:
        assert _status(conn, a) == "ready"
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and b in st.behind
        verdict = ksb.unblock_verdict(conn, a)
        assert not verdict.startswith("still held"), verdict
        assert ksb.shared_blocker_guard(conn, a) is None
        # The wall still holds every other card behind it.
        assert _status(conn, b) == "scheduled"
        assert ksb.release_cleared_blockers(conn) == []


def test_own_wake_is_spent_by_one_run(kanban_home, monkeypatch, gateway):
    """After one run the card is an ordinary card behind X again (e.g. that run was reclaimed)."""
    a, _b = _own_probe_fires(monkeypatch, gateway, HOME)
    with kbc.connect_closing() as conn:
        assert kb.claim_task(conn, a)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready', current_run_id = NULL WHERE id = ?", (a,))
        assert ksb.unblock_verdict(conn, a).startswith("still held"), ksb.unblock_verdict(conn, a)


def test_wake_from_a_park_that_awaits_the_wall_is_still_held(kanban_home, monkeypatch, gateway):
    """A park whose awaiting names the wall waits on the wall: its probe's exit 0 does not lift the hold."""
    a, _b = _own_probe_fires(monkeypatch, gateway, HOME, awaits_wall=True)
    with kbc.connect_closing() as conn:
        assert _status(conn, a) == "ready"
        assert ksb.unblock_verdict(conn, a).startswith("still held"), ksb.unblock_verdict(conn, a)
