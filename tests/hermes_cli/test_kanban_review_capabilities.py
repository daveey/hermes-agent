"""Review handoffs enforce real profile policy before mutating task/run history."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_capabilities import CapabilityMismatchError


LOCAL = "platform_toolsets: {cli: [terminal, file]}\nterminal: {backend: local}\n"


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def configure(name, policy=LOCAL):
        target = home / "profiles" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.yaml").write_text(policy)

    configure("implementer")
    configure("reviewer")
    kb.init_db()
    return configure


def _prepare(conn, operation, workspace_kind):
    tid = kb.create_task(
        conn, title="Capability-checked review", assignee="implementer",
        workspace_kind=workspace_kind, workspace_path=None,
        validate_workspace=False,
        requires=["terminal"] if workspace_kind == "scratch" else [],
    )
    assert kb.claim_task(conn, tid) is not None
    if operation != "request":
        assert kb.request_review(
            conn, tid, reviewer="reviewer",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        if operation != "reopen":
            assert kb.claim_review_task(conn, tid) is not None
        if operation == "rerequest":
            assert kb.request_changes(conn, tid, reason="Revise")[0]
            assert kb.claim_task(conn, tid) is not None
    return tid


def _handoff(conn, tid, operation):
    run_id = kb.get_task(conn, tid).current_run_id
    if operation in {"request", "rerequest"}:
        return kb.request_review(
            conn, tid, reviewer="reviewer" if operation == "request" else None,
            expected_run_id=run_id,
        )
    if operation == "changes":
        return kb.request_changes(conn, tid, reason="Revise", expected_run_id=run_id)[0]
    return kb.reopen_review_task(conn, tid)


@pytest.mark.parametrize("operation", ["request", "rerequest", "changes", "reopen"])
@pytest.mark.parametrize("workspace_kind", ["scratch", "dir", "worktree"])
def test_review_rejects_incapable_target_atomically(profiles, operation, workspace_kind):
    with kbc.connect() as conn:
        tid = _prepare(conn, operation, workspace_kind)
        target = "reviewer" if operation in {"request", "rerequest"} else "implementer"
        # Policy can change after creation or the previous handoff. Remote terminal
        # access satisfies explicit terminal needs but not host workspace access.
        policy = (
            "platform_toolsets: {cli: [file]}\n"
            if workspace_kind == "scratch" else LOCAL.replace("local", "ssh")
        )
        profiles(target, policy)
        before = list(conn.iterdump())
        with pytest.raises(CapabilityMismatchError) as caught:
            _handoff(conn, tid, operation)
        assert caught.value.assignee == target
        assert caught.value.missing == (["terminal"] if workspace_kind == "scratch" else ["host_repo"])
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("operation", ["request", "rerequest", "changes", "reopen"])
def test_review_accepts_capable_target(profiles, operation):
    with kbc.connect() as conn:
        tid = _prepare(conn, operation, "worktree")
        assert _handoff(conn, tid, operation)
        task = kb.get_task(conn, tid)
        reviewing = operation in {"request", "rerequest"}
        assert task.assignee == ("reviewer" if reviewing else "implementer")
        assert task.status == ("review" if reviewing else "ready")
        assert task.current_run_id is None
