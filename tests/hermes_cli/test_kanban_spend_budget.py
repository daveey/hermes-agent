"""Spend budget: a card that keeps spending runs without handing work on goes to triage. [daveey]

Repro of the 2026-11-14 long tail (t_01a1d975f729f802): t_01a1a1dea9c64d6a ran 85 times in 7 days and was
still ``todo``; its runs alternated continued / blocked / scheduled / reclaimed, so neither the no-progress
budget (trailing waits only, reset by any edit, link or immediate continue) nor the continuation cap
(back-to-back ``continued`` only) ever stopped it. Nothing bounded the total spend of one card.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_capability_registry as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_spend_budget as ksb
from hermes_cli import kanban_wake_check as kwc

LIMITS = ksb.Limits(runs=ksb.DEFAULT_RUNS, cache_read_tokens=ksb.DEFAULT_CACHE_READ)


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
    monkeypatch.setattr(kwc, "run_due_wake_checks", lambda conn, *a, **k: [])
    monkeypatch.setattr(kcr, "maybe_refresh_async", lambda *a, **k: None)
    monkeypatch.setattr(ksb, "limits", lambda cfg=None: LIMITS)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


class Spawns:
    def __init__(self):
        self.ids: list[str] = []

    def __call__(self, task, workspace, board=None):
        self.ids.append(task.id)


_CLOCK = {"t": 0}


def _run(conn, tid: str, outcome: str = "continued", *, seconds: int = 600, usage: dict | None = None) -> int:
    """One closed past run of ``tid`` (``seconds`` long), with an optional ``worker_result`` usage receipt.
    Runs are laid out back to back, ending before now, so every one is inside the read cutoff."""
    _CLOCK["t"] += seconds + 5
    end = int(time.time()) - 400_000 + _CLOCK["t"]
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'default', ?, ?, ?, ?)", (tid, outcome, end - seconds, end, outcome))
        run_id = int(cur.lastrowid)
        if usage is not None:
            kb._append_event(conn, tid, "worker_result", {"pid": 1, "claim_lock": "x", "exit_code": 0,
                                                          "last_output": "", "usage": usage}, run_id=run_id)
    return run_id


def _runs(conn, tid: str, n: int, **kw) -> None:
    for i in range(n):
        _run(conn, tid, ("continued", "blocked", "scheduled", "reclaimed")[i % 4], **kw)


def _events(conn, tid: str, kind: str) -> list:
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def test_long_tail_card_goes_to_triage_with_its_spend(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Retention safe ladder", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS, usage={"cache_read_tokens": 1_200_000, "input_tokens": 30_000,
                                                  "output_tokens": 9_000, "cost_usd": 0.5})
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert spawns.ids == []
        assert res.no_progress_held == [(tid, "spend_budget:triage")]
        assert kb.get_task(conn, tid).status == "triage"
        (event,) = _events(conn, tid, ksb.EVENT)
        assert event.payload["limit"] == "runs" and event.payload["runs"] == ksb.DEFAULT_RUNS
        assert event.payload["tokens"]["cache_read_tokens"] == 18_000_000
        assert event.payload["runs_with_usage"] == ksb.DEFAULT_RUNS and event.payload["cost_usd"] == 7.5
        (comment,) = kb.list_comments(conn, tid)
        assert comment.author == ksb.AUTHOR
        assert "15 runs since" in comment.body and "18.0M cache-read" in comment.body and "~$7.50" in comment.body
        # Triage is a hold: the next tick spawns nothing and writes nothing.
        assert kbd.dispatch_once(conn, spawn_fn=spawns).no_progress_held == [] and spawns.ids == []
        assert len(_events(conn, tid, ksb.EVENT)) == 1


def test_below_budget_still_spawns(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="busy", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS - 1)
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert spawns.ids == [tid] and res.no_progress_held == []


def test_cache_read_limit_trips_before_the_run_limit(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="deep reader", assignee="default")
        _runs(conn, tid, 2, usage={"cache_read_tokens": 14_000_000})
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        _run(conn, tid, usage={"cache_read_tokens": 2_000_000})
        assert ksb.hold(conn, tid, lim=LIMITS) == "triage"
        (event,) = _events(conn, tid, ksb.EVENT)
        assert event.payload["limit"] == "cache_read_tokens" and event.payload["runs"] == 3
        assert "30.0M cache-read tokens" in event.payload["reason"]


def test_runs_that_spent_nothing_do_not_count(kanban_home):
    """Quota walls, released claims and spawn failures end in seconds: they cost no tokens."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="walled", assignee="default")
        for outcome in ("rate_limited", "released", "spawn_failed") * 10:
            _run(conn, tid, outcome, seconds=ksb.MIN_RUN_SECONDS - 1)
        _runs(conn, tid, ksb.DEFAULT_RUNS - 1)
        assert ksb.spend(conn, tid).runs == ksb.DEFAULT_RUNS - 1
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        _run(conn, tid, "rate_limited", seconds=ksb.MIN_RUN_SECONDS)  # a mid-run wall did spend
        assert ksb.hold(conn, tid, lim=LIMITS) == "triage"


@pytest.mark.parametrize("handoff", ksb.RESET_OUTCOMES)
def test_a_hand_off_opens_a_new_window(kanban_home, handoff):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="reviewed often", assignee="default")
        _runs(conn, tid, 10, usage={"cache_read_tokens": 2_000_000})
        _run(conn, tid, handoff, usage={"cache_read_tokens": 2_000_000})
        _runs(conn, tid, ksb.DEFAULT_RUNS - 1, usage={"cache_read_tokens": 1_000_000})
        spent = ksb.spend(conn, tid)
        assert spent.runs == ksb.DEFAULT_RUNS - 1 and spent.tokens["cache_read_tokens"] == 14_000_000
        assert ksb.hold(conn, tid, lim=LIMITS) is None


def test_newest_usage_receipt_per_run_wins(kanban_home):
    """A run's receipts are cumulative session counters: two receipts for one run are not added up."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        run_id = _run(conn, tid, usage={"cache_read_tokens": 5_000_000})
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_result", {"exit_code": 0, "usage": {"cache_read_tokens": 7_000_000}},
                             run_id=run_id)
            kb._append_event(conn, tid, "worker_result", {"exit_code": 1, "last_output": "no usage"}, run_id=run_id)
        _run(conn, tid)  # no receipt: a run, no tokens
        spent = ksb.spend(conn, tid)
        assert (spent.runs, spent.reported, spent.tokens["cache_read_tokens"]) == (2, 1, 7_000_000)


def test_malformed_usage_values_are_ignored(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _run(conn, tid, usage={"cache_read_tokens": "lots", "input_tokens": -5, "output_tokens": True,
                               "cache_write_tokens": 10, "cost_usd": "x"})
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                         "VALUES (?, ?, 'worker_result', ?, ?)", (tid, 1, '{"usage": [1, 2]}', int(time.time())))
        spent = ksb.spend(conn, tid)
        assert spent.tokens == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 10}
        assert spent.cost_usd == 0


def test_promote_from_triage_starts_a_new_window(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        assert ksb.hold(conn, tid, lim=LIMITS) == "triage"
        ok, why = kb.promote_task(conn, tid, actor="david", reason="worth it")
        assert ok, why
        assert ksb.spend(conn, tid).runs == 0
        spawns = Spawns()
        kbd.dispatch_once(conn, spawn_fn=spawns)
        assert spawns.ids == [tid]


def test_live_claim_is_never_taken_off_the_lane(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_lock = 'other:1', claim_expires = ? WHERE id = ?",
                         (int(time.time()) + 600, tid))
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        assert kb.get_task(conn, tid).status == "ready" and _events(conn, tid, ksb.EVENT) == []
        with kb.write_txn(conn):  # an expired claim is no longer live
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, tid))
        assert ksb.hold(conn, tid, lim=LIMITS) == "triage"


def test_only_ready_or_review_cards_move(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        for status, moved in (("todo", False), ("blocked", False), ("review", True)):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            assert (ksb.hold(conn, tid, lim=LIMITS) == "triage") is moved, status


def test_hand_off_between_read_and_lock_wins(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        real = kb.write_txn

        def racing(c, **kw):
            monkeypatch.setattr(kb, "write_txn", real)
            _run(c, tid, "completed")
            return real(c, **kw)

        monkeypatch.setattr(kb, "write_txn", racing)
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        assert kb.get_task(conn, tid).status == "ready"


def test_disabled_limits_and_foreign_copies_spawn(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS, usage={"cache_read_tokens": 40_000_000})
        assert ksb.hold(conn, tid, lim=ksb.Limits(0, 0)) is None
        assert ksb.hold(conn, tid, lim=ksb.Limits(0, 50_000_000_000)) is None
        assert ksb.hold(conn, tid, lim=ksb.Limits(100, 0)) is None
        monkeypatch.setattr(kwc, "foreign_copy", lambda c, t: True)
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        assert kb.get_task(conn, tid).status == "ready"


def test_dry_run_tick_moves_nothing(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        kbd.dispatch_once(conn, spawn_fn=Spawns(), dry_run=True)
        assert kb.get_task(conn, tid).status == "ready" and _events(conn, tid, ksb.EVENT) == []


def test_limits_read_config():
    assert ksb.limits({}) == (ksb.DEFAULT_RUNS, ksb.DEFAULT_CACHE_READ)
    assert ksb.limits({"kanban": {"spend_budget_runs": 0, "spend_budget_cache_read_tokens": "5"}}) == (0, 5)
    assert ksb.limits({"kanban": {"spend_budget_runs": "x", "spend_budget_cache_read_tokens": None}}) == (
        ksb.DEFAULT_RUNS, ksb.DEFAULT_CACHE_READ)
    assert ksb.limits({"kanban": {"spend_budget_runs": -3}}).runs == 0


def test_limits_survive_an_unreadable_config(monkeypatch):
    from hermes_cli import config

    monkeypatch.setattr(config, "load_config", lambda: (_ for _ in ()).throw(OSError("gone")))
    assert ksb.limits() == (ksb.DEFAULT_RUNS, ksb.DEFAULT_CACHE_READ)


class _Agent:
    session_input_tokens = 1200
    session_output_tokens = 300
    session_cache_read_tokens = 2_500_000
    session_cache_write_tokens = 0
    session_estimated_cost_usd = 1.23456
    provider = "anthropic"
    model = "claude-opus-5-5"


def test_usage_fields_from_the_agent():
    assert ksb.usage_fields(_Agent()) == {"usage": {"input_tokens": 1200, "output_tokens": 300,
                                                    "cache_read_tokens": 2_500_000, "cost_usd": 1.2346}}
    assert ksb.usage_fields(None) == {}
    assert ksb.usage_fields(object()) == {}

    class Weird:
        session_input_tokens = True
        session_cache_read_tokens = "9"
        session_estimated_cost_usd = False

    assert ksb.usage_fields(Weird()) == {}


def test_managed_worker_receipt_carries_the_run_usage(kanban_home):
    """Producer: the managed worker's ``worker_result`` (written on the host that ran the turn) books the
    agent's session tokens, so the card's home board can sum them whichever worker ran each attempt."""
    from gateway import session_kanban

    db = kb.kanban_db_path()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        assert kb.claim_task(conn, tid, claimer="host:1")
        task = kb.get_task(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound", {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    context = {"db": str(db), "task_id": tid, "run_id": task.current_run_id, "claim_lock": task.claim_lock}
    session_kanban._record_worker_result(context, 0, "done", None, agent=_Agent())
    with kbc.connect_closing() as conn:
        (event,) = _events(conn, tid, "worker_result")
        assert event.payload["usage"]["cache_read_tokens"] == 2_500_000
        assert event.payload["route_provider"] == "anthropic"


def test_payload_json_roundtrip(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        ksb.hold(conn, tid, lim=LIMITS)
        raw = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                           (tid, ksb.EVENT)).fetchone()[0]
        assert json.loads(raw)["limits"] == {"runs": ksb.DEFAULT_RUNS, "cache_read_tokens": ksb.DEFAULT_CACHE_READ}


# --- t_01a1daf392bf6005: a human answer re-authorizes; a card can carry its own limit --------------------

def _blocked_and_answered(conn, tid: str, *, writer_kind: str, relay: bool = False) -> None:
    """The card blocked needs_input after spending its budget; someone answered and unblocked it."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "which ring?"})
    kwargs = {"relayed_human": {"session": "chat-1", "message_id": 7, "at": int(time.time())}} if relay else {}
    kb.add_comment(conn, tid, "david" if writer_kind == "human" else "default", "The left ring.",
                   writer_kind=writer_kind, **kwargs)
    assert kb.unblock_task(conn, tid)


@pytest.mark.parametrize("writer_kind, relay", [("human", False), ("agent", True)], ids=["human", "relayed"])
def test_a_human_answer_opens_a_new_window(kanban_home, writer_kind, relay):
    """Defect 1: David answered a needs_input card that had spent its budget while blocked; the next tick
    triaged it with no run, undoing his answer. A human (or verified relayed) answer now opens a new window."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="asked David", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        _blocked_and_answered(conn, tid, writer_kind=writer_kind, relay=relay)
        assert ksb.spend(conn, tid).runs == 0
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert res.no_progress_held == [] and spawns.ids == [tid]
        assert _events(conn, tid, ksb.EVENT) == []


def test_an_agent_comment_and_a_bare_unblock_do_not_reopen_the_budget(kanban_home):
    """Agent notes are what a loop produces, and a bare unblock carries no actor: neither opens a window."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="loops", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        _blocked_and_answered(conn, tid, writer_kind="agent")
        kb.add_comment(conn, tid, "default", "legacy note without a kind")
        spawns = Spawns()
        assert kbd.dispatch_once(conn, spawn_fn=spawns).no_progress_held == [(tid, "spend_budget:triage")]
        assert spawns.ids == [] and kb.get_task(conn, tid).status == "triage"


def test_per_card_exemption_is_explicit_and_audited(kanban_home):
    """Defect 2: a standing shepherd card went to triage every 15 runs. An explicit per-card override
    (0 = no limit) lets it run, and it is recorded as an event naming who set it and why."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Upkeep shepherd", assignee="default")
        other = kb.create_task(conn, title="Upkeep shepherd", assignee="default")  # same title: not exempt
        ksb.set_override(conn, tid, runs=0, cache_read_tokens=0, by="david", reason="standing shepherd")
        for card in (tid, other):
            _runs(conn, card, ksb.DEFAULT_RUNS * 3, usage={"cache_read_tokens": 2_000_000})
        spawns = Spawns()
        res = kbd.dispatch_once(conn, spawn_fn=spawns)
        assert spawns.ids == [tid] and res.no_progress_held == [(other, "spend_budget:triage")]
        (event,) = _events(conn, tid, ksb.OVERRIDE_EVENT)
        assert event.payload == {"runs": 0, "cache_read_tokens": 0, "by": "david", "reason": "standing shepherd"}
        assert any("standing shepherd" in c.body and "david" in c.body for c in kb.list_comments(conn, tid))


def test_per_card_limit_override_newest_wins_and_clears(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="long", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        ksb.set_override(conn, tid, runs=40, by="david", reason="big migration")
        assert ksb.card_limits(conn, tid, LIMITS) == (40, ksb.DEFAULT_CACHE_READ)  # unset field keeps the board's
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        ksb.set_override(conn, tid, by="david", reason="back to normal")  # nothing set = board limits again
        assert ksb.card_limits(conn, tid, LIMITS) == LIMITS
        assert ksb.hold(conn, tid, lim=LIMITS) == "triage"
        (event,) = _events(conn, tid, ksb.EVENT)
        assert event.payload["limits"] == {"runs": ksb.DEFAULT_RUNS, "cache_read_tokens": ksb.DEFAULT_CACHE_READ}
        assert event.payload["override"] is False


def test_a_lowered_card_limit_trips_and_says_so(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="cheap", assignee="default")
        ksb.set_override(conn, tid, runs=3, by="david", reason="should be quick")
        _runs(conn, tid, 3)
        assert ksb.hold(conn, tid, lim=LIMITS) == "triage"
        (event,) = _events(conn, tid, ksb.EVENT)
        assert event.payload["limits"]["runs"] == 3 and event.payload["override"] is True
        assert "this card's override" in event.payload["reason"]


def test_a_card_limit_applies_when_the_board_disables_the_budget(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="cheap", assignee="default")
        ksb.set_override(conn, tid, runs=3, by="david", reason="should be quick")
        _runs(conn, tid, 3)
        assert ksb.hold(conn, tid, lim=ksb.Limits(0, 0)) == "triage"


def test_override_set_between_read_and_lock_wins(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        _runs(conn, tid, ksb.DEFAULT_RUNS)
        real = kb.write_txn

        def racing(c, **kw):
            monkeypatch.setattr(kb, "write_txn", real)
            ksb.set_override(c, tid, runs=0, cache_read_tokens=0, by="david", reason="standing")
            return real(c, **kw)

        monkeypatch.setattr(kb, "write_txn", racing)
        assert ksb.hold(conn, tid, lim=LIMITS) is None
        assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize("kw", [
    {"runs": -1}, {"cache_read_tokens": "lots"}, {"runs": True}, {"reason": " "}, {"by": ""},
])
def test_override_refuses_bad_input(kanban_home, kw):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        with pytest.raises(ValueError):
            ksb.set_override(conn, tid, **{"by": "david", "reason": "why", **kw})
        assert _events(conn, tid, ksb.OVERRIDE_EVENT) == []


def test_override_refuses_an_unknown_card(kanban_home):
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            ksb.set_override(conn, "t_missing", runs=1, by="david", reason="why")


def test_malformed_override_payload_falls_back_to_board_limits(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, ksb.OVERRIDE_EVENT, {"runs": "x", "cache_read_tokens": -4})
        assert ksb.card_limits(conn, tid, LIMITS) == LIMITS
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, 'nope', ?)",
                         (tid, ksb.OVERRIDE_EVENT, int(time.time())))
        assert ksb.card_limits(conn, tid, LIMITS) == LIMITS


def test_cli_sets_and_shows_the_card_override(kanban_home):
    from hermes_cli import kanban as kc

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Upkeep shepherd", assignee="default")
    out = kc.run_slash(f"spend-budget {tid} --exempt --reason 'standing shepherd'")
    assert "no spend budget" in out, out
    shown = json.loads(kc.run_slash(f"spend-budget {tid} --json"))
    assert shown["override"]["runs"] == 0 and shown["override"]["reason"] == "standing shepherd"
    assert shown["limits"] == {"runs": 0, "cache_read_tokens": 0}
    assert "--reason" in kc.run_slash(f"spend-budget {tid} --runs 30")  # a change needs a reason
    kc.run_slash(f"spend-budget {tid} --runs 30 --reason 'bounded after all'")
    with kbc.connect_closing() as conn:
        assert ksb.card_limits(conn, tid, LIMITS) == (30, ksb.DEFAULT_CACHE_READ)
    kc.run_slash(f"spend-budget {tid} --clear --reason done")
    with kbc.connect_closing() as conn:
        assert ksb.card_limits(conn, tid, LIMITS) == LIMITS
        assert len(_events(conn, tid, ksb.OVERRIDE_EVENT)) == 3
    assert "unknown" in kc.run_slash("spend-budget t_missing --exempt --reason x")
    assert "--exempt" in kc.run_slash(f"spend-budget {tid} --exempt --runs 3 --reason x")
