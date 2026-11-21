"""Provider wall scope survives each public park entry point on an isolated real board."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tools import kanban_tools  # registers real entry points
from tools.registry import registry

SCOPE = {"blocker_provider": "anthropic", "blocker_model": "claude-test", "blocker_lane": "review"}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    assert kb.kanban_db_path().is_relative_to(tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="provider metadata test", assignee="default")
        assert kb.claim_task(conn, tid)
        yield conn, tid


def call(tool, tid, **extra):
    args = {"task_id": tid, "blocker_id": "provider-wall"}
    if tool == "kanban_continue":
        args["checkpoint"] = "Wait for provider reset"
    else:
        args.update(reason="Wait for provider reset", clock_only=True)
        if tool == "kanban_block":
            args["kind"] = "time"
    args.update(extra)
    result = json.loads(registry.dispatch(tool, args))
    assert result.get("ok"), result


def persisted(conn, tid):
    run = kb.list_runs(conn, tid)[-1]
    events = [e for e in kb.list_events(conn, tid)
              if e.kind in ("blocked", "scheduled", "continued", "dependency_wait")]
    assert run.metadata is not None
    return run.metadata, events[-1].payload


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
def test_scope_round_trip(board, tool):
    conn, tid = board
    call(tool, tid, **SCOPE)
    for data in persisted(conn, tid):
        assert data["blocker_id"] == "provider-wall"
        assert {key: data[key] for key in SCOPE} == SCOPE


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
@pytest.mark.parametrize("field", list(SCOPE))
def test_invalid_scope_refused_without_transition(board, tool, field):
    conn, tid = board
    args = {"task_id": tid, "blocker_id": "provider-wall", field: 123,
            "checkpoint" if tool == "kanban_continue" else "reason": "wait"}
    if tool != "kanban_continue":
        args["clock_only"] = True
    if tool == "kanban_block":
        args["kind"] = "time"
    result = json.loads(registry.dispatch(tool, args))
    assert field in result.get("error", ""), result
    assert kb.get_task(conn, tid).status == "running"


def route(conn, tid):
    """Historical concrete run receipt (disposable-DB seed: neutral admission now records class/auto)."""
    from hermes_cli import kanban_model_tier as tier
    run_id = kb._current_run_id(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, tier.RUN_ROUTE_EVENT,
                         {"model": "gpt-test", "provider": "openai-codex", "lane": "ready",
                          "source": "pin", "tier_reason": "legacy_receipt"}, run_id=run_id)
    receipt = [e.payload for e in kb.list_events(conn, tid) if e.kind == tier.RUN_ROUTE_EVENT]
    assert receipt and receipt[-1]["provider"] == "openai-codex"


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
@pytest.mark.parametrize("error", ["ModelRoutingError: No eligible deep routes under billing/capability policy",
                                   "provider quota exceeded"])
def test_unlabelled_wall_never_borrows_current_receipt(board, tool, error):
    """A wall marker with no provider label stays unscoped even though this run's receipt is Codex."""
    conn, tid = board
    route(conn, tid)
    key = "checkpoint" if tool == "kanban_continue" else "reason"
    call(tool, tid, **{key: error})
    for data in persisted(conn, tid):
        assert data["blocker_id"] == "provider-wall"
        assert not set(SCOPE).intersection(data)


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
def test_stored_run_error_never_borrows_current_receipt(board, tool):
    """An unlabelled wall in this run's stored error (e.g. a delegated child's provider) is not
    the executor's route: the receipt says Codex, but the park stays unscoped."""
    conn, tid = board
    route(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET error=? WHERE id=?",
                     ("ModelRoutingError: No eligible deep routes", kb._current_run_id(conn, tid)))
    call(tool, tid)
    for data in persisted(conn, tid):
        assert data["blocker_id"] == "provider-wall"
        assert not set(SCOPE).intersection(data)


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
def test_later_step_error_does_not_borrow_current_route(board, tool):
    conn, tid = board
    route(conn, tid)
    key = "checkpoint" if tool == "kanban_continue" else "reason"
    call(tool, tid, **{key: "Later reviewer hit ModelRoutingError: route unavailable"})
    for data in persisted(conn, tid):
        assert "blocker_provider" not in data
        assert "blocker_model" not in data
        assert "blocker_lane" not in data


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
def test_provider_labelled_error_is_not_current_run_identity(board, tool):
    conn, tid = board
    route(conn, tid)
    key = "checkpoint" if tool == "kanban_continue" else "reason"
    call(tool, tid, **{key: "ModelRoutingError: provider=anthropic model=claude-test unavailable in later review"})
    for data in persisted(conn, tid):
        assert data["blocker_provider"] == "anthropic"
        assert data["blocker_model"] == "claude-test"
        assert "blocker_lane" not in data


def test_explicit_scope_wins_over_error(board):
    conn, tid = board
    route(conn, tid)
    call("kanban_continue", tid, checkpoint="provider quota exceeded provider=openai-codex model=gpt-test",
         **SCOPE)
    assert all({key: data[key] for key in SCOPE} == SCOPE for data in persisted(conn, tid))


def test_plain_dependency_block_persists_scope(board):
    conn, tid = board
    parent = kb.create_task(conn, title="unfinished parent", assignee="default")
    kb.link_tasks(conn, parent, tid, expected_child_run_id=kb._current_run_id(conn, tid))
    assert kb.block_task(conn, tid, reason="provider wall", kind="dependency", blocker_id="provider-wall", **SCOPE)
    assert all({key: data[key] for key in SCOPE} == SCOPE for data in persisted(conn, tid))


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
def test_unscoped_legacy_wall_remains_unscoped(board, tool):
    conn, tid = board
    route(conn, tid)
    call(tool, tid)
    for data in persisted(conn, tid):
        assert data["blocker_id"] == "provider-wall"
        assert not set(SCOPE).intersection(data)


def test_error_without_receipt_never_uses_current_task_pin(board):
    conn, tid = board
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET provider_override='anthropic', model_override='claude-test' WHERE id=?", (tid,))
    call("kanban_continue", tid, checkpoint="ModelRoutingError: unavailable")
    assert all("blocker_provider" not in data for data in persisted(conn, tid))


@pytest.mark.parametrize("error, expected", [
    ("provider quota exceeded provider=anthropic model=claude-test", "anthropic"),
    ("ModelRoutingError: provider=anthropic provider=openai-codex", None),
    # A provider label on a line with no wall marker is prose, not wall identity.
    ("Ran the impl on provider=openai-codex.\nLater reviewer: usage limit reached", None),
])
def test_reason_identity_over_run_receipt(board, error, expected):
    conn, tid = board
    route(conn, tid)
    call("kanban_continue", tid, checkpoint=error)
    assert all(data.get("blocker_provider") == expected for data in persisted(conn, tid))


def test_untyped_block_classification_records_scope(board):
    conn, tid = board
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="crashed")
        conn.execute("UPDATE tasks SET status='blocked', current_run_id=NULL WHERE id=?", (tid,))
    parent = kb.create_task(conn, title="unfinished parent", assignee="default")
    kb.link_tasks(conn, parent, tid)
    assert kb.block_task(conn, tid, kind="dependency", reason="provider wall",
                         blocker_id="provider-wall", **SCOPE)
    assert all({key: data[key] for key in SCOPE} == SCOPE for data in persisted(conn, tid))


def test_direct_continuation_preserves_metadata_scope(board):
    from hermes_cli.kanban_continuation import continue_task
    conn, tid = board
    assert continue_task(conn, tid, checkpoint="provider quota exceeded provider=openai-codex",
                         metadata={"blocker_id": "provider-wall", **SCOPE})["ok"]
    assert all({key: data[key] for key in SCOPE} == SCOPE for data in persisted(conn, tid))


# --- Inference names only known MODEL providers; explicit scope is never filtered. ---

@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
@pytest.mark.parametrize("text", [
    "GitHub API rate limit exceeded (provider: github)",
    "rate limit exceeded provider=not-a-real-provider model=x",
])
def test_unknown_provider_label_is_not_inferred(board, tool, text):
    """A service wall labelled with a non-model provider stays board-wide (unscoped)."""
    conn, tid = board
    route(conn, tid)
    key = "checkpoint" if tool == "kanban_continue" else "reason"
    call(tool, tid, **{key: text})
    for data in persisted(conn, tid):
        assert data["blocker_id"] == "provider-wall"
        assert not set(SCOPE).intersection(data)


@pytest.mark.parametrize("tool", ["kanban_block", "kanban_continue", "kanban_schedule"])
@pytest.mark.parametrize("text, expected", [
    ("Anthropic rate limit exceeded (provider: anthropic)", "anthropic"),
    ("usage limit reached provider=openai-codex model=gpt-test", "openai-codex"),
    ("usage limit reached provider=claude", "anthropic"),   # registry alias, canonical id stored
])
def test_known_model_provider_label_still_inferred(board, tool, text, expected):
    conn, tid = board
    key = "checkpoint" if tool == "kanban_continue" else "reason"
    call(tool, tid, **{key: text})
    for data in persisted(conn, tid):
        assert data["blocker_provider"] == expected


def test_unknown_and_known_label_on_wall_lines_is_ambiguous(board):
    conn, tid = board
    call("kanban_continue", tid, checkpoint="rate limit exceeded provider=github\n"
                                            "usage limit reached provider=anthropic")
    assert all("blocker_provider" not in data for data in persisted(conn, tid))


@pytest.mark.parametrize("via", ["argument", "metadata"])
def test_explicit_scope_is_never_filtered_by_registry(board, via):
    """Explicit blocker_provider keeps its pre-inference compatibility, known or not."""
    from hermes_cli.kanban_continuation import continue_task
    conn, tid = board
    text = "GitHub API rate limit exceeded (provider: github)"
    if via == "argument":
        call("kanban_continue", tid, checkpoint=text, blocker_provider="github")
    else:
        assert continue_task(conn, tid, checkpoint=text,
                             metadata={"blocker_id": "provider-wall", "blocker_provider": "github"})["ok"]
    assert all(data["blocker_provider"] == "github" for data in persisted(conn, tid))
