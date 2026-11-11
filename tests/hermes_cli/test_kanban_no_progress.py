"""No-progress budget: a card whose runs only park again stops being re-spawned. [daveey]

Repro of t_e3e216d4 (141 runs, Oct 23 - Nov 10): the shepherd parked on another card
(``awaiting kind=card`` + ``wait-check task``), its clock wake fired before that card moved
(``continuation_woken`` 98 times vs ``condition_met`` 8), and each spawned worker re-read the same
graph, commented "reconciliation complete, rollout NOT complete" and parked again. Card waits are
exempt from the 3x re-park escalation (their producer is accountable), and the continuation cap only
counts back-to-back ``continued`` runs, so nothing bounded the loop.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_capability_registry as kcr
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_no_progress as knp
from hermes_cli import kanban_resume_requester as krr
from hermes_cli import kanban_wake_check as kwc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "3600")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Probes and capability refreshes run real commands off-thread; the clock wake is what is under test.
    monkeypatch.setattr(kwc, "run_due_wake_checks", lambda conn, *a, **k: [])
    monkeypatch.setattr(kcr, "maybe_refresh_async", lambda *a, **k: None)
    monkeypatch.setattr(knp, "limit", lambda cfg=None: knp.DEFAULT_LIMIT)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


class Spawns:
    def __init__(self):
        self.ids: list[str] = []

    def __call__(self, task, workspace, board=None):
        self.ids.append(task.id)


def _clock_wake(conn) -> list[str]:
    """The clock wake ``wake_due_continuations`` gives every scheduled card once its wake_at passes."""
    return kc.wake_due_continuations(conn, now=int(time.time()) + 30 * 86400)


def _card_wait_cycle(conn, tid: str, producer: str) -> None:
    """One t_e3e216d4 run: claimed, found the producer unchanged, parked on it again, clock-woken."""
    assert kb.claim_task(conn, tid)
    assert kb.schedule_task(conn, tid, reason="Reconciliation complete, rollout NOT complete; waiting on producer",
                            awaiting={"kind": "card", "ref": producer})
    assert tid in _clock_wake(conn)


def _shepherd(conn, cycles: int) -> tuple[str, str]:
    producer = kb.create_task(conn, title="E: rollout", assignee="other")
    tid = kb.create_task(conn, title="Shepherd the graph", assignee="default")
    for _ in range(cycles):
        _card_wait_cycle(conn, tid, producer)
    return tid, producer


def _events(conn, tid: str, kind: str) -> list:
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def test_card_wait_loop_parks_behind_its_own_condition(kanban_home):
    with kbc.connect_closing() as conn:
        tid, producer = _shepherd(conn, knp.DEFAULT_LIMIT)
        assert kb.get_task(conn, tid).status == "ready"
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert tid not in spawns.ids, "a 6th run was spawned into the same unchanged graph"
        assert res.no_progress_held == [(tid, "parked")]
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        park = _events(conn, tid, "scheduled")[-1].payload
        assert park["wake_check"] == f"hermes kanban wait-check task {producer}"
        assert park["wake_at"] >= int(time.time()) + knp.REPARK_SECONDS - 60
        assert park["no_progress"]["runs"] == knp.DEFAULT_LIMIT
        assert park["awaiting"]["ref"] == producer
        row = conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()
        assert row and row[0] == park["wake_check"]
        assert len(_events(conn, tid, knp.PARKED_EVENT)) == 1
        # Its probe's condition is the way back: a met condition is progress, so the next wake spawns.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "condition_met", {"wake_check": park["wake_check"]})
        assert kbd.dispatch_once(conn, spawn_fn=spawns).no_progress_held == []
        assert tid in spawns.ids


def test_below_budget_still_spawns(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = _shepherd(conn, knp.DEFAULT_LIMIT - 1)
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert tid in spawns.ids and res.no_progress_held == []


def test_clock_refallback_reparks_without_spawning(kanban_home):
    """The 24 h clock fallback brings the card back to the lane; with still no progress it re-parks, no run."""
    with kbc.connect_closing() as conn:
        tid, _ = _shepherd(conn, knp.DEFAULT_LIMIT)
        spawns = Spawns()
        kbd.dispatch_once(conn, spawn_fn=spawns)
        assert tid in _clock_wake(conn)
        runs_before = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
        kbd.dispatch_once(conn, spawn_fn=spawns)
        assert tid not in spawns.ids
        assert kb.get_task(conn, tid).status == "scheduled"
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == runs_before


def _human(conn, tid):
    kb.add_comment(conn, tid, "daveey", "Use the other region.", writer_kind="human")


def _linked_done(conn, tid):
    parent = kb.create_task(conn, title="dep", assignee="other")
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, tid))
        kb._append_event(conn, parent, "completed", {})


def _linked_changes_requested(conn, tid):
    child = kb.create_task(conn, title="impl", assignee="other")
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (tid, child))
        kb._append_event(conn, child, "changes_requested", {})


def _attached(conn, tid):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "attached", {"filename": "proof.log"})


@pytest.mark.parametrize("progress", [_human, _linked_done, _linked_changes_requested, _attached])
def test_progress_resets_the_budget(kanban_home, progress):
    with kbc.connect_closing() as conn:
        tid, _ = _shepherd(conn, knp.DEFAULT_LIMIT)
        progress(conn, tid)
        spawns = Spawns()
        assert kbd.dispatch_once(conn, spawn_fn=spawns).no_progress_held == []
        assert tid in spawns.ids


def test_agent_comments_are_not_progress(kanban_home):
    """The loop's own output ("NO_PROOF retained", "no duplicate owner") never resets it."""
    with kbc.connect_closing() as conn:
        tid, _ = _shepherd(conn, knp.DEFAULT_LIMIT)
        kb.add_comment(conn, tid, "default", "Reconciled: NO_PROOF retained.", writer_kind="agent")
        kb.add_comment(conn, tid, "default", "Supervisor disposition: no duplicate owner.")
        spawns = Spawns()
        assert kbd.dispatch_once(conn, spawn_fn=spawns).no_progress_held == [(tid, "parked")]


# --- synthetic run histories for the counting rules -----------------------------------------

def _run(conn, tid: str, outcome: str, park: dict | None = None, *, at: int | None = None) -> int:
    at = int(time.time()) if at is None else at
    with kb.write_txn(conn):
        rid = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'default', ?, ?, ?, ?)", (tid, outcome, at, at, outcome)).lastrowid
        if park is not None:
            kind = "continued" if outcome == "continued" else "scheduled"
            kb._append_event(conn, tid, kind, park, run_id=rid)
            conn.execute("UPDATE task_events SET created_at = ? WHERE run_id = ?", (at, rid))
    return rid


COND = {"wake_at": 1, "wake_check": "exit 1", "check_every": 900}
CONT_WAIT = {**COND, "status": "scheduled"}


def _card(conn, title="loop") -> str:
    tid = kb.create_task(conn, title=title, assignee="default")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def test_counting_rules(kanban_home):
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(4):
            _run(conn, t, "scheduled", COND)
        for neutral in ("crashed", "reclaimed", "rate_limited", "released"):
            _run(conn, t, neutral)
        assert knp.streak(conn, t)[0] == 4, "run losses must neither count nor reset"
        _run(conn, t, "continued", CONT_WAIT)
        assert knp.streak(conn, t)[0] == 5, "a continue into a wait is a waiting run"
        _run(conn, t, "scheduled", {"wake_at": 1, "clock_only": True})
        _run(conn, t, "scheduled", {"wake_at": 1, "awaiting": {"kind": "clock", "ref": "soak window"}})
        assert knp.streak(conn, t)[0] == 5, "a declared time window's wakes are the measurement itself"
        _run(conn, t, "scheduled", {"wake_at": 1})  # a clock poll with nothing to probe
        assert knp.streak(conn, t)[0] == 5 + 1 / knp.CLOCK_POLL_WEIGHT
        _run(conn, t, "blocked")
        assert knp.streak(conn, t)[0] == 6 + 1 / knp.CLOCK_POLL_WEIGHT

        long_work = _card(conn, "long build")
        for _ in range(6):
            _run(conn, long_work, "scheduled", COND)
        _run(conn, long_work, "continued", {"status": "ready", "wake_at": 1})  # soft-deadline checkpoint
        assert knp.streak(conn, long_work)[0] == 0
        handed_on = _card(conn, "reviewed")
        for _ in range(6):
            _run(conn, handed_on, "scheduled", COND)
        _run(conn, handed_on, "review_requested")
        assert knp.streak(conn, handed_on)[0] == 0


def test_clock_polls_need_the_weighted_budget(kanban_home):
    with kbc.connect_closing() as conn:
        t = _card(conn)
        polls = knp.DEFAULT_LIMIT * knp.CLOCK_POLL_WEIGHT
        for _ in range(polls - 1):
            _run(conn, t, "scheduled", {"wake_at": 1})
        assert knp.hold(conn, t) is None
        _run(conn, t, "scheduled", {"wake_at": 1})
        assert knp.hold(conn, t).startswith("escalated:")


def test_blocked_loop_files_one_owner_card_and_no_recovery_loop(kanban_home):
    with kbc.connect_closing() as conn:
        t = _card(conn, "goal card")
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "blocked")
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert t not in spawns.ids
        [(held, action)] = res.no_progress_held
        owner = action.split(":", 1)[1]
        assert held == t and kb.get_task(conn, t).status == "todo"
        o = kb.get_task(conn, owner)
        assert o.created_by == knp.CREATED_BY and "Do not unblock or requeue" in o.body
        assert conn.execute("SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (owner, t)).fetchone()
        assert not {e.kind for e in kb.list_events(conn, t)} & {"continuation_exhausted", "goal_unachievable"}
        assert krr.ensure_recovery_cards(conn) == []
        # Requeued unchanged by someone: the same episode reuses the same owner card.
        conn.execute("DELETE FROM task_links WHERE parent_id = ?", (owner,))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        conn.commit()
        assert knp.hold(conn, t) == f"escalated:{owner}"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by = ?", (knp.CREATED_BY,)).fetchone()[0] == 1


def test_looping_owner_card_goes_to_triage_not_another_owner(kanban_home):
    with kbc.connect_closing() as conn:
        o = kb.create_task(conn, title="Unstick x", assignee="default", created_by=knp.CREATED_BY)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (o,))
        conn.commit()
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, o, "blocked")
        assert knp.hold(conn, o) == "triage"
        assert kb.get_task(conn, o).status == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by = ?", (knp.CREATED_BY,)).fetchone()[0] == 1


def test_broken_probe_escalates_instead_of_reparking(kanban_home):
    """t_01a1a27deb10165f: 591 runs, 588 ``wake_check_broken`` wakes; parking behind that probe re-wakes it."""
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "scheduled", COND)
        assert knp.decide(conn, t, budget=knp.DEFAULT_LIMIT)[0] == "park"
        with kb.write_txn(conn):
            kb._append_event(conn, t, "wake_check_broken", {"rc": 127})
        assert knp.decide(conn, t, budget=knp.DEFAULT_LIMIT, as_of=int(time.time()) + 2)[0] == "escalate"


def test_clock_only_park_is_no_condition(kanban_home):
    """A declared time window that also carries a probe is still a time window: nothing to park behind."""
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "blocked")
        with kb.write_txn(conn):
            kb._append_event(conn, t, "scheduled", {"wake_at": 1, "wake_check": "exit 1", "clock_only": True})
        assert knp.decide(conn, t, budget=knp.DEFAULT_LIMIT, as_of=int(time.time()) + 2)[0] == "escalate"


def test_condition_from_before_the_last_progress_is_not_reused(kanban_home):
    with kbc.connect_closing() as conn:
        t = _card(conn)
        old = int(time.time()) - 100
        _run(conn, t, "scheduled", {**COND, "wake_check": "hermes kanban wait-check task OLD"}, at=old)
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'edited', '{}', ?)",
                         (t, old + 10))
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "blocked", at=old + 20)
        assert knp.decide(conn, t, budget=knp.DEFAULT_LIMIT)[0] == "escalate"


def test_an_earlier_broken_probe_does_not_veto_the_current_one(kanban_home):
    with kbc.connect_closing() as conn:
        t = _card(conn)
        base = int(time.time()) - 100
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                         "VALUES (?, 'wake_check_broken', '{}', ?)", (t, base))
        for i in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "scheduled", COND, at=base + 10 + i)
        assert knp.decide(conn, t, budget=knp.DEFAULT_LIMIT)[0] == "park"


def test_live_claim_is_never_taken_off_the_lane(kanban_home):
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "scheduled", COND)
        conn.execute("UPDATE tasks SET claim_lock = 'other', claim_expires = ? WHERE id = ?",
                     (int(time.time()) + 600, t))
        conn.commit()
        assert knp.hold(conn, t) is None
        assert kb.get_task(conn, t).status == "ready"
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, t))
        conn.commit()
        assert knp.hold(conn, t) == "parked"


def test_progress_between_read_and_lock_wins(kanban_home, monkeypatch):
    """A human comment that lands after the cheap read but before the write lock keeps the card spawnable."""
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "scheduled", COND, at=int(time.time()) - 10)
        real = knp.decide
        calls = []

        def racing(c, tid, **kw):
            calls.append(tid)
            if len(calls) == 1:
                verdict = real(c, tid, **kw)
                kb.add_comment(conn, t, "daveey", "use the other region", writer_kind="human")
                return verdict
            return real(c, tid, **kw)

        monkeypatch.setattr(knp, "decide", racing)
        assert knp.hold(conn, t) is None and len(calls) == 2
        assert kb.get_task(conn, t).status == "ready"


def test_neutral_rows_never_hide_the_streak(kanban_home):
    """250 crashes between the waits: no row cap stops the count short of the budget."""
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "scheduled", COND)
            for _ in range(50):
                _run(conn, t, "crashed")
        assert knp.streak(conn, t)[0] == knp.DEFAULT_LIMIT


def test_disabled_and_foreign_copies_spawn(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        t = _card(conn)
        for _ in range(knp.DEFAULT_LIMIT):
            _run(conn, t, "scheduled", COND)
        assert knp.hold(conn, t, budget=0) is None
        monkeypatch.setattr(kwc, "foreign_copy", lambda c, tid: tid == t)
        assert knp.hold(conn, t) is None
        assert kb.get_task(conn, t).status == "ready"


def test_limit_reads_config():
    assert knp.limit({"kanban": {"no_progress_runs": 0}}) == 0
    assert knp.limit({"kanban": {"no_progress_runs": "7"}}) == 7
    assert knp.limit({"kanban": {"no_progress_runs": "x"}}) == knp.DEFAULT_LIMIT
    assert knp.limit({}) == knp.DEFAULT_LIMIT


def test_dry_run_tick_moves_nothing(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = _shepherd(conn, knp.DEFAULT_LIMIT)
        res = kbd.dispatch_once(conn, spawn_fn=Spawns(), dry_run=True)
        assert res.no_progress_held == [] and kb.get_task(conn, tid).status == "ready"


def test_payload_json_roundtrip(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = _shepherd(conn, knp.DEFAULT_LIMIT)
        kbd.dispatch_once(conn, spawn_fn=Spawns())
        raw = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                           (tid, knp.PARKED_EVENT)).fetchone()[0]
        assert json.loads(raw)["runs"] == knp.DEFAULT_LIMIT
