"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking —
  unless no parent is open, in which case the wait can never be satisfied
  and the block is recorded as ``needs_input`` (sticky, loop-counted).
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", parents=()):
    """Create a task (linked under ``parents`` first) and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    for parent in parents:
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(
            conn,
            parent_id=parent,
            child_id=child,
            expected_child_run_id=kb.get_task(conn, child).current_run_id,
        )
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_block_with_terminal_parents_parks_then_escalates(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``dependency`` block whose parents are all terminal can never be
    satisfied by ``recompute_ready``: it must park in ``blocked`` as
    ``needs_input`` (no ``dependency_wait``, no re-promotion), say so on the
    CLI, and count toward the loop breaker so a re-block after an unblock
    reaches ``triage``."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="already-done-parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        # The edge exists before the run: link_tasks refuses to gate a running child retroactively.
        child = _running_task(conn, title="child-of-done", parents=(parent,))

        # `hermes kanban block <child> --kind dependency waiting on upstream`
        args = argparse.Namespace(task_id=child, ids=None, reason=["waiting", "on", "upstream"], kind="dependency")
        assert kanban_cli._cmd_block(args) == 0
        assert "needs_input" in capsys.readouterr().out
        parked = kb.get_task(conn, child)
        assert (parked.status, parked.block_kind, parked.block_recurrences) == ("blocked", "needs_input", 1)
        events = kb.list_events(conn, child)
        assert not [e for e in events if e.kind == "dependency_wait"]
        blocked = [e for e in events if e.kind == "blocked"][-1].payload
        assert (blocked["requested_kind"], blocked["rekind_reason"]) == ("dependency", "no_open_parent")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "blocked"

        # A cron/human unblocks; the worker re-declares the same impossible wait.
        assert kb.unblock_task(conn, child)
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.block_task(conn, child, reason="still waiting", kind="dependency")
        assert kb.get_task(conn, child).status == "triage"
        loop = [e for e in kb.list_events(conn, child) if e.kind == "block_loop_detected"][-1].payload
        assert loop["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT


def test_dependency_block_with_open_parent_stays_parked_across_dispatch_tick(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    """Control: a genuine wait on an incomplete parent parks in ``todo``
    without counting a recurrence, survives a dispatch tick unspawned, and
    resumes once the parent finishes."""
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open-parent", assignee="alice")
        # Linked while todo (a running child cannot be gated retroactively); the parent then stays
        # open while the child runs — the reopened-parent shape, forced the same way the loop below does.
        child = kb.create_task(conn, title="waiter", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.block_task(conn, child, reason="wait", kind="dependency")
            parked = kb.get_task(conn, child)
            assert (parked.status, parked.block_kind, parked.block_recurrences) == ("todo", "dependency", 0)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        assert kb.block_task(conn, child, reason="wait", kind="dependency")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert kb.get_task(conn, child).status == "todo"
        assert child not in spawns
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="alice")
        kb.complete_task(conn, parent, result="done")
        res2 = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert child in [row[0] for row in res2.spawned]


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Auto-decomposer must not re-promote a block-loop escalation without human input
# ---------------------------------------------------------------------------


def _loop_to_triage(conn) -> str:
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="needs David", kind="needs_input")
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="needs David", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_auto_decompose_sweep_skips_block_loop_escalation(kanban_home: Path) -> None:
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        looped = _loop_to_triage(conn)
        plain = kb.create_task(conn, title="rough idea", triage=True)
        assert kb.awaiting_human_after_block_loop(conn, looped) is True
        assert kb.awaiting_human_after_block_loop(conn, plain) is False

    swept = decomp.list_triage_ids(skip_awaiting_human=True)
    assert plain in swept
    assert looped not in swept
    # ``decompose --all`` is a sweep too: it skips the escalation as well.
    assert looped not in decomp.list_triage_ids()


def test_human_comment_after_escalation_does_not_reenable_auto_decompose(kanban_home: Path) -> None:
    """A comment on a block-loop escalation is the human working the acceptance
    step, not a request for a new spec: the sweep must still leave it alone
    (it re-decomposed a shipped card into 5 re-implementation children)."""
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        looped = _loop_to_triage(conn)
        kb.add_comment(conn, looped, "david", "Try channel 11 on the new AP.")
        assert kb.decomposition_refusal(conn, looped) is not None

    assert looped not in decomp.list_triage_ids(skip_awaiting_human=True)


def _ran_then_triaged(conn) -> str:
    """A card that has run and reached triage by some path other than a block loop."""
    tid = _running_task(conn, title="shipped feature")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage', current_run_id=NULL, claim_lock=NULL "
                     "WHERE id=?", (tid,))
    return tid


def test_decomposer_refuses_card_that_already_ran(kanban_home: Path, monkeypatch) -> None:
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        ran = _ran_then_triaged(conn)
        fresh = kb.create_task(conn, title="rough idea", triage=True)
        assert kb.decomposition_refusal(conn, fresh) is None
        assert "already run" in (kb.decomposition_refusal(conn, ran) or "")

    calls = []
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **k: calls.append(a) or (None, "no aux"))
    assert ran not in decomp.list_triage_ids(skip_awaiting_human=True)
    assert fresh in decomp.list_triage_ids(skip_awaiting_human=True)
    outcome = decomp.decompose_task(ran, author="auto-decomposer")
    assert outcome.ok is False and "already run" in outcome.reason
    assert calls == []  # refused before any LLM call
    # An explicit --force still reaches the LLM.
    decomp.decompose_task(ran, author="david", force=True)
    assert len(calls) == 1


def test_cli_decompose_threads_force_flag(kanban_home: Path, monkeypatch) -> None:
    from hermes_cli import kanban_decompose as decomp

    seen = []

    def fake(tid, *, author=None, timeout=None, force=False):
        seen.append(force)
        return decomp.DecomposeOutcome(tid, False, "stub")

    monkeypatch.setattr(decomp, "decompose_task", fake)
    for force in (False, True):
        args = argparse.Namespace(task_id="t_x", all_triage=False, author="david",
                                  json=False, tenant=None, force=force)
        kanban_cli._cmd_decompose(args)
    assert seen == [False, True]


def test_needs_input_reblock_with_new_reason_is_progress_not_a_loop(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="Approve the Google sign-in on your iPhone", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="Now tap Allow on the MacBook prompt", kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_recurrences == 1
        # Same question again (only whitespace/case differs) IS a loop.
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="now tap allow on the  MacBook prompt", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"


def test_unblock_moves_block_loop_triage_card_back_to_ready(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        looped = _loop_to_triage(conn)
        assert kb.unblock_task(conn, looped) is True
        assert kb.get_task(conn, looped).status == "ready"
        # Plain blocked/scheduled behaviour is unchanged; unknown/done still refused.
        assert kb.unblock_task(conn, looped) is False


def test_promote_moves_triage_card_to_ready_with_audit(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        looped = _loop_to_triage(conn)
        ok, err = kb.promote_task(conn, looped, actor="david", reason="human step done")
        assert ok, err
        assert kb.get_task(conn, looped).status == "ready"
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='promoted_manual'",
                          (looped,)).fetchone()
        assert '"from_status": "triage"' in ev[0]
        # Parent gating still applies to a triage card.
        parent = kb.create_task(conn, title="p", assignee="worker")
        child = kb.create_task(conn, title="c", triage=True)
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        ok, err = kb.promote_task(conn, child, actor="david")
        assert not ok and "unsatisfied parent" in err


def test_dispatcher_auto_decompose_tick_requests_human_skip(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    import hermes_cli
    from gateway import kanban_watchers_dispatcher as kwd

    seen = {}

    def fake_list(**kw):
        seen.update(kw)
        return []

    fake = SimpleNamespace(list_triage_ids=fake_list, decompose_task=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)
    monkeypatch.setattr(kwd, "_board_slugs", lambda kb_: ["default"])
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    disp = kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)
    assert disp.auto_decompose_tick(5) == 0
    assert seen.get("skip_awaiting_human") is True
