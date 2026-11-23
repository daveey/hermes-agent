"""C5 consequential readers: real disposable dispatch regressions adapted from the probe120 scenarios.

Every board is the isolated tmp board of the ``board`` fixture. "Seeded" marks state written raw into the temp
DB because ingress now refuses it (a FOREIGN legacy executor copy, which the retirement sweep skips). The
route-resolution fault is injected: no natural trigger is known, so a live worker/natural fault stays unproven.
"""
from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli.test_kanban_model_tier import _review_source, _write_cfg
from tests.hermes_cli.test_kanban_model_tier import board as board  # noqa: PLC0414 -- self-alias suppresses F811 for the fixture parameter

RAW_MODEL, RAW_PROVIDER = "claude-opus-5-5", "anthropic"   # the only provider the fixture's class routes use
QUOTA_LOG = "quota exhausted (429); retry after 7200s"


def _legacy(conn, tid, model=RAW_MODEL, provider=RAW_PROVIDER, mandatory=None):
    """Seeded legacy pin on a FOREIGN executor copy (home = other gateway): the retirement sweep skips it."""
    from hermes_cli import kanban_sync_client as client
    client.ensure_identity(conn, "default", "owner")
    conn.execute("UPDATE tasks SET created_on=? WHERE id=?", ("other-gateway", tid))
    conn.execute("UPDATE tasks SET model_override=?, provider_override=?, model_pin_mandatory=? WHERE id=?",
                 (model, provider, mandatory, tid))
    conn.commit()


def _row(conn, tid):
    return tuple(conn.execute("SELECT model_override, provider_override, model_pin_mandatory FROM tasks WHERE id=?",
                              (tid,)).fetchone())


def _events(conn, tid, kind):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == kind]


def _spawn_once(conn):
    seen = {}
    kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, ws: seen.__setitem__(
        task.id, (task.model_override, task.provider_override, getattr(task, "model_pin_mandatory", None))))
    return seen


def _fault(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("subset130 injected route-resolution fault")
    monkeypatch.setattr(tier, "resolve_dispatch_tier", boom)


def _log(board):
    path = board / "t.log"
    path.write_text(QUOTA_LOG)
    return path


def _sibling_routes(conn):
    from hermes_cli.kanban_class_walls import class_wall_routes
    sib = kb.get_task(conn, kb.create_task(conn, title="neutral sibling", assignee="default"))
    return class_wall_routes(sib, "class:standard", now=time.time())


def _rate_limited(conn, tid, run_id, **fields):
    """Seeded reaper classification of a rate-limited exit; ``fields`` adds the attempt's own quota receipt."""
    if fields:
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "worker_result", {"pid": 71001, "claim_lock": task.claim_lock,
                                                      "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, **fields},
                         run_id=run_id)
    conn.execute("UPDATE task_runs SET outcome='rate_limited', status='rate_limited', ended_at=? WHERE id=?",
                 (int(time.time()), run_id))
    conn.commit()


@pytest.fixture
def wallfile(board, monkeypatch):
    _write_cfg(board, sync={"enabled": True})
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    p = board / "kanban" / "provider_walls.json"
    monkeypatch.setattr(walls, "walls_path", lambda: p)
    monkeypatch.setattr(walls, "profile_provider", lambda: "")
    return p


# --- provider walls: raw stored provider_override fallback removed (P1/P5/P7) --------------------------------


def test_no_receipt_wall_never_reads_raw_stored_provider(board, wallfile):
    """P1+P7: no run receipt and no profile provider: nothing is walled and a neutral sibling keeps its route."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    _legacy(conn, tid)
    assert walls.record_wall_for_run(conn, tid, None, _log(board)) is None
    assert walls.active_walls(path=wallfile) == {}
    routes, known = _sibling_routes(conn)
    assert [r["provider"] for r in routes] == [RAW_PROVIDER] and known == {}
    assert _row(conn, tid) == (RAW_MODEL, RAW_PROVIDER, None)   # stored author evidence untouched


def test_no_receipt_wall_falls_back_to_profile_provider(board, wallfile, monkeypatch):
    """Profile fallback order kept: it may name a provider the run never used (no universal correct-wall claim)."""
    monkeypatch.setattr(walls, "profile_provider", lambda: "xai")
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    _legacy(conn, tid)
    assert walls.record_wall_for_run(conn, tid, None, _log(board)) is not None
    assert set(walls.active_walls(path=wallfile)) == {"xai"}


def test_model_tier_receipt_still_precedes_profile(board, wallfile, monkeypatch):
    """Control: the run's own model_tier receipt provider walls before the profile fallback."""
    monkeypatch.setattr(walls, "profile_provider", lambda: "xai")
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    _legacy(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "model_tier", {"reason": "pinned", "model": "gpt-6-astra",
                                                   "provider": "openai-codex", "lane": "ready"}, run_id=None)
    walls.record_wall_for_run(conn, tid, None, _log(board))
    assert set(walls.active_walls(path=wallfile)) == {"openai-codex"}


def test_fault_without_episode_never_walls_raw_column(board, wallfile, monkeypatch):
    """P5: fault receipt, no episode, no model_tier event: the stored xai pin is not walled."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="legacy", assignee="default")
    _legacy(conn, tid, model="grok-4.7", provider="xai")
    _fault(monkeypatch)
    _spawn_once(conn)
    run_id = kb.get_task(conn, tid).current_run_id
    assert walls.record_wall_for_run(conn, tid, run_id, _log(board)) is None
    assert walls.active_walls(path=wallfile) == {}


# --- tier fault path: effective selector on the in-memory task, mandatory cleared (P3/P4) ----------------------


def test_healthy_dispatch_receipts_unchanged(board, wallfile):
    """P2 control: healthy admission of a legacy pin is already class/auto; nothing walls."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    _legacy(conn, tid)
    seen = _spawn_once(conn)
    run_id = kb.get_task(conn, tid).current_run_id
    assert seen[tid][:2] == ("class:standard", "auto")
    mt, rr = _events(conn, tid, "model_tier"), _events(conn, tid, "run_route")
    assert mt[-1]["provider"] == "auto" and rr[-1]["source"] == "class" and rr[-1]["provider"] is None
    assert walls.record_wall_for_run(conn, tid, run_id, _log(board)) is None
    assert walls.active_walls(path=wallfile) == {}


def test_fault_spawn_uses_effective_selector_and_clears_mandatory(board, wallfile, monkeypatch):
    """P3: injected fault (non-review): spawn and run_route carry the class selector, not the raw pin;
    the stored row (pin and mandatory) is unchanged."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    _legacy(conn, tid, mandatory=1)
    _fault(monkeypatch)
    seen = _spawn_once(conn)
    task = kb.get_task(conn, tid)
    assert seen[tid] == ("class:standard", "auto", None)
    assert task.status == "running"
    assert _row(conn, tid) == (RAW_MODEL, RAW_PROVIDER, 1)
    assert _events(conn, tid, "model_tier") == []
    rr = _events(conn, tid, "run_route")[-1]
    assert (rr["model"], rr["provider"], rr["source"], rr["tier_reason"]) == ("class:standard", None, "class", "error")
    from hermes_cli import kanban_shared_blockers as sb
    assert sb._run_route(conn, tid, task.current_run_id)[:2] == ("class:standard", None)
    assert tier.owner_model_pin(conn, task) == ("class:standard", "auto")


def test_fault_receipt_gives_no_raw_episode_or_wall(board, wallfile, monkeypatch):
    """P4: fault receipt + rate-limited end without an attempt receipt: the episode names no provider,
    nothing is walled, and a neutral sibling keeps its class route."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="legacy", assignee="default")
    _legacy(conn, tid)
    _fault(monkeypatch)
    _spawn_once(conn)
    run_id = kb.get_task(conn, tid).current_run_id
    _rate_limited(conn, tid, run_id)
    from hermes_cli.kanban_quota_episode import episode, record_episode
    state = episode(conn, tid)
    assert state and state["run_id"] == run_id
    assert state["provider"] is None and state["providers"] == [] and state["model"] == "class:standard"
    with kb.write_txn(conn):
        record_episode(conn, tid, run_id)
    assert walls.record_wall_for_run(conn, tid, run_id, _log(board)) is None
    assert walls.active_walls(path=wallfile) == {}
    routes, known = _sibling_routes(conn)
    assert routes and known == {}


def test_fault_with_concrete_allocation_receipt_still_walls_genuine_provider(board, wallfile, monkeypatch):
    """A real attempt receipt (the provider that actually ran) still establishes the wall; the stored foreign
    xai pin is not added to the episode's providers."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="legacy", assignee="default")
    _legacy(conn, tid, model="grok-4.7", provider="xai")
    _fault(monkeypatch)
    _spawn_once(conn)
    run_id = kb.get_task(conn, tid).current_run_id
    _rate_limited(conn, tid, run_id, route_provider=RAW_PROVIDER, route_model=RAW_MODEL,
                  last_output="Anthropic usage limit resets in ~11h")
    from hermes_cli.kanban_quota_episode import episode
    state = episode(conn, tid)
    assert state["provider"] == RAW_PROVIDER and state["providers"] == [RAW_PROVIDER]
    assert walls.record_wall_for_run(conn, tid, run_id, _log(board)) is not None
    assert set(walls.active_walls(path=wallfile)) == {RAW_PROVIDER}
    assert _sibling_routes(conn)[0] == []


def test_neutral_and_legacy_fault_receipts_match(board, wallfile, monkeypatch):
    """DESIGN121 §5: a neutral card's fault receipt moves from unknown to class; legacy and neutral agree."""
    conn = kbc.connect()
    legacy = kb.create_task(conn, title="legacy", assignee="default")
    _legacy(conn, legacy)
    neutral = kb.create_task(conn, title="neutral", assignee="default")
    _fault(monkeypatch)
    seen = _spawn_once(conn)
    assert seen[legacy] == seen[neutral] == ("class:standard", "auto", None)
    receipts = [{k: v for k, v in _events(conn, tid, "run_route")[-1].items() if k != "ratio"}
                for tid in (legacy, neutral)]
    assert receipts[0] == receipts[1] == {"model": "class:standard", "provider": None, "lane": "ready",
                                          "source": "class", "tier_reason": "error"}
    assert kb.get_task(conn, neutral).model_override is None   # stored neutral row unchanged


def test_unknown_route_receipt_still_unknown(board, wallfile, monkeypatch):
    """Control: a receipt whose route cannot be resolved stays unknown (None), never a guess."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    _fault(monkeypatch)

    def unresolvable(_task):
        raise RuntimeError("route unknown")
    monkeypatch.setattr(tier, "effective_route", unresolvable)
    _spawn_once(conn)
    rr = _events(conn, tid, "run_route")[-1]
    assert (rr["model"], rr["provider"], rr["source"]) == (None, None, "unknown")
    from hermes_cli import kanban_shared_blockers as sb
    assert sb._run_route(conn, tid, kb.get_task(conn, tid).current_run_id)[:2] == (None, None)


def test_review_fault_still_refuses_without_receipt(board, monkeypatch):
    """Control: the review exception is unchanged: a review resolution fault refuses, never an error route."""
    from hermes_cli.kanban_review_model import ReviewModelUnavailable
    conn = kbc.connect()
    tid = kb.create_task(conn, title="implement", assignee="default")
    _review_source(conn, tid)
    task = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    _fault(monkeypatch)
    before = (task.model_override, task.provider_override)
    with pytest.raises(ReviewModelUnavailable, match="Review model resolution failed"):
        tier.apply_model_tier(conn, task, lane="review")
    assert (task.model_override, task.provider_override) == before
    assert [e for e in kb.list_events(conn, tid)
            if e.kind == "run_route" and e.run_id == task.current_run_id] == []
