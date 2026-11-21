"""Deferred-placement forwarder recovery (split from ``test_kanban_placement``). [daveey]

The requeue loops (request still queued, refused with no local fallback, a failed forward step) park on a declared
clock that still escalates on the third identical requeue; quoted hub/exception text never refuses that park; and
when a forward step and its requeue both fail, ``main()`` holds a sent or qualified-origin card instead of running it
here.
"""
from __future__ import annotations

import json

import pytest

from tests.hermes_cli.test_kanban_placement import _done_parent, _forwarder

pytest_plugins = ["tests.hermes_cli.test_kanban_placement"]  # its ``mac`` fixture (fake hub + board)


@pytest.mark.parametrize("loop", ["queued", "refused_no_fallback", "step_failed"])
def test_placement_requeue_loops_escalate_on_the_third_requeue(mac, monkeypatch, loop):
    """The forwarder's requeue loops park on a declared clock (``_park_clock``). Declared clocks are exempt from the
    repeat-park escalation for agents, but these system loops pass ``count_repeats``: the third requeue goes to an
    owner card instead of re-queueing every 10 min forever. The per-attempt detail in the why (an exception text)
    does not split the premise. [daveey]"""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    if loop == "refused_no_fallback":  # a qualified origin's card: no local fallback, so _cancel re-parks
        real_state = kpf.state
        monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), "origin_gateway": "elsewhere"})
    actions = []
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        for n in range(3):
            f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
            if loop == "queued":
                actions.append(f.run())
            elif loop == "refused_no_fallback":
                actions.append(f._cancel(f"hub refused the card: attempt {n}"))
            else:
                actions.append(f._park_clock(f"forward step failed (RuntimeError: timeout {n}); retrying"))
            if n < 2:
                assert kb.get_task(conn, child).status == "scheduled", (n, actions)
                kb.unblock_task(conn, child)
                kb.recompute_ready(conn)
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert kinds.count("scheduled") == 2 and kaw.ESCALATED_EVENT in kinds, (actions, kinds)
        assert kb.get_task(conn, child).status == "todo"


# A hub refusal or exception text quoted in a requeue park reads like an agent wall ("permission denied", "waiting
# for David"); the wall check refused the park, main()'s retry raised again and ran a qualified-origin card here.
WALL_TEXT = "refused: permission denied, needs access granted; waiting for David to approve the profile"


@pytest.mark.parametrize("loop", ["not_live", "refused_no_fallback", "step_failed"])
def test_placement_requeue_parks_whatever_the_quoted_text_says(mac, monkeypatch, loop):
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    assert kaw.text_wall(WALL_TEXT)  # the text an agent park would be refused for
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    if loop == "refused_no_fallback":
        real_state = kpf.state
        monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), "origin_gateway": "elsewhere"})
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        if loop == "not_live":
            f.live = lambda t: WALL_TEXT
            action = f.run()
        elif loop == "refused_no_fallback":
            action = f._cancel(f"hub refused the card: {WALL_TEXT}")
        else:
            action = f._park_clock(f"forward step failed (RuntimeError: {WALL_TEXT}); retrying")
        assert action == "requeued", action
        assert kb.get_task(conn, child).status == "scheduled"


@pytest.mark.parametrize("shape", ["qualified_origin", "already_sent"])
def test_main_never_runs_a_qualified_or_sent_card_locally_when_the_requeue_fails(mac, monkeypatch, shape):
    """main(): the forward step raises and the requeue raises too. A card with no local fallback (qualified origin)
    or already sent is held (blocked, agent-owned), never exec'd as a local worker. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    real_state = kpf.state
    extra = {"origin_gateway": "elsewhere"} if shape == "qualified_origin" else {"request_id": "rq-1"}
    monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), **extra})
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    def boom(self, *a, **kw):
        raise RuntimeError(f"hub said: {WALL_TEXT}")
    monkeypatch.setattr(kpf.Forwarder, "run", boom)
    monkeypatch.setattr(kpf.Forwarder, "_park_clock", boom)
    monkeypatch.setattr(kpf.os, "execvp", lambda *a: pytest.fail("ran the card locally"))
    assert kpf.main(["--", "hermes", "worker"]) == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, child)
        assert task.status in ("blocked", "triage") and task.block_kind == "transient", (task.status, task.block_kind)
        blocked = [e for e in kb.list_events(conn, child) if e.kind == "blocked"]
        assert blocked and "must not run here" in blocked[-1].payload["reason"]


def test_main_exits_nonzero_without_running_the_card_when_even_the_hold_fails(mac, monkeypatch):
    """main(): forward, requeue and the hold's block all fail. The card stays claimed for the dispatcher's crash
    path (exit 1); it is never exec'd here. A SCHEDULED_UNTIL= marker in the quoted error does not turn the hold
    into a timed park. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    real_state = kpf.state
    monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), "request_id": "rq-1"})
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    def boom(self, *a, **kw):
        raise RuntimeError("hub said: retry SCHEDULED_UNTIL=2026-11-21T08:00:00Z")
    monkeypatch.setattr(kpf.Forwarder, "run", boom)
    monkeypatch.setattr(kpf.Forwarder, "_park_clock", boom)
    monkeypatch.setattr(kpf.os, "execvp", lambda *a: pytest.fail("ran the card locally"))
    # The marker alone no longer breaks the hold: it is held.
    assert kpf.main(["--", "hermes", "worker"]) == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, child)
        assert task.status in ("blocked", "triage") and task.block_kind == "transient"
        assert "SCHEDULED_UNTIL=" not in [e for e in kb.list_events(conn, child) if e.kind == "blocked"][-1].payload["reason"]

    # And when the block itself fails: exit 1, still no local run.
    child2 = json.loads(kt._handle_create({"title": "Fix the other lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed2 = kb.claim_task(conn, child2)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child2)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed2.current_run_id))
    monkeypatch.setattr(kb, "block_task", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("board locked")))
    assert kpf.main(["--", "hermes", "worker"]) == 1
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, child2).status == "running"


def test_main_still_falls_back_for_an_unsent_local_card_when_cancel_fails(mac, monkeypatch):
    """Control: a card never sent and with a local fallback still runs here when the forward and its cancel fail."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    def boom(self, *a, **kw):
        raise RuntimeError("transport down")
    monkeypatch.setattr(kpf.Forwarder, "run", boom)
    monkeypatch.setattr(kpf.Forwarder, "_cancel", boom)
    ran = []
    monkeypatch.setattr(kpf.os, "execvp", lambda *a: ran.append(a))
    assert kpf.main(["--", "hermes", "worker"]) == 1 and ran
