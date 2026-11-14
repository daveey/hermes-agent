"""Landing disposition enforced through both public completion entry paths."""
import json
import shlex

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
pytest_plugins = ["tests.hermes_cli.test_kanban_pr_acceptance"]

PR = "https://github.com/daveey/hermes/pull/621"
SUMMARY = "It is open and unmerged; landing and deploy ride the hourly hub batch per the card."


def complete(surface, tid, metadata, monkeypatch):
    if surface == "cli":
        from hermes_cli import kanban
        return kanban.run_slash(f"complete {tid} --summary {shlex.quote(SUMMARY)} --metadata {shlex.quote(json.dumps(metadata))}")
    from tools.registry import registry
    import tools.kanban_tools  # register the actual tool handler
    with connect() as conn:
        run_id = kb.claim_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return registry.dispatch("kanban_complete", {"summary": SUMMARY, "metadata": metadata})


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_open_pr_cannot_complete_on_hourly_batch_promise(github, monkeypatch, surface):
    github["pr_state"] = "OPEN"
    with connect() as conn:
        tid = kb.create_task(conn, title="Replay t_01a19a73526daaa9 (known head a4733f8e)", completion_contract=PR)
    output = complete(surface, tid, {}, monkeypatch)
    with connect() as conn:
        assert kb.get_task(conn, tid).status != "done", output
    assert "landing_owner" in output
    assert "hub batch" in output


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_terminal_pr_disposition(github, monkeypatch, surface, state):
    github["pr_state"] = state
    with connect() as conn:
        tid = kb.create_task(conn, title="Terminal PR", completion_contract=PR)
    output = complete(surface, tid, {}, monkeypatch)
    with connect() as conn:
        assert kb.get_task(conn, tid).status == "done", output


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_left_open_is_recorded_and_visible_in_done_notice(github, monkeypatch, surface):
    from types import SimpleNamespace
    from gateway.kanban_watchers_notifier import _fmt_completed
    github["pr_state"] = "OPEN"
    reason = "User requested a draft for later evaluation"
    with connect() as conn:
        tid = kb.create_task(conn, title="Draft", completion_contract=PR)
    output = complete(surface, tid, {"pr_left_open": {"reason": reason}}, monkeypatch)
    with connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "done", output
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0])
    assert payload["pr_left_open"] == {"reason": reason}
    notice, wake, _ = _fmt_completed(SimpleNamespace(payload=payload), SimpleNamespace(task=task, head="Done", title="Draft"))
    for text in (notice, wake):
        assert reason in text
        assert "hub batch only installs merged main" in text
        assert "never merges PRs" in text


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("owner_kind", ["ready", "done", "archived", "self", "wrong_pr", "missing"])
def test_landing_owner_must_be_open_same_board_exact_pr(github, monkeypatch, surface, owner_kind):
    github["pr_state"] = "OPEN"
    with connect() as conn:
        tid = kb.create_task(conn, title="Implementation", completion_contract=PR)
        owner = kb.create_task(conn, title="Landing", completion_contract=PR + "0" if owner_kind == "wrong_pr" else PR)
        if owner_kind in {"done", "archived"}:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (owner_kind, owner))
        if owner_kind == "self":
            owner = tid
        if owner_kind == "missing":
            owner = "t_not_on_this_board"
    output = complete(surface, tid, {"landing_owner": owner}, monkeypatch)
    with connect() as conn:
        assert (kb.get_task(conn, tid).status == "done") is (owner_kind == "ready"), output


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("change", ["done", "archived", "wrong_pr"])
def test_owner_changes_after_prepare_are_rechecked_in_write_transaction(github, monkeypatch, surface, change):
    from hermes_cli import kanban_pr_acceptance_store as store
    github["pr_state"] = "OPEN"
    with connect() as conn:
        tid = kb.create_task(conn, title="Implementation", completion_contract=PR)
        owner = kb.create_task(conn, title="Landing", completion_contract=PR)
    prepare = store.prepare_acceptance
    def raced_prepare(conn, *args):
        receipt = prepare(conn, *args)
        assert receipt[1]["landing"] == {"landing_owner": owner}
        with connect() as rival:
            if change == "wrong_pr":
                rival.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (PR + "0", owner))
            else:
                rival.execute("UPDATE tasks SET status=? WHERE id=?", (change, owner))
        return receipt
    monkeypatch.setattr(store, "prepare_acceptance", raced_prepare)
    output = complete(surface, tid, {"landing_owner": owner}, monkeypatch)
    with connect() as conn:
        assert kb.get_task(conn, tid).status != "done", output
    assert "landing_owner" in output


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("mode", ["local_publication", "waiver", "blank_reason", "string_reason", "read_error"])
def test_no_landing_bypasses(github, monkeypatch, surface, mode):
    from hermes_cli import kanban_pr_acceptance as acceptance
    github["pr_state"] = "OPEN"
    metadata = {"published_pr": PR}
    if mode == "waiver":
        github.update(no_required=True, no_ci=True)
        metadata["contract_waiver"] = {"reason": "Local tests green"}
    if mode == "blank_reason":
        metadata["pr_left_open"] = {"reason": " "}
    if mode == "string_reason":
        metadata["pr_left_open"] = "not the required object"
    if mode == "read_error":
        def unavailable(*args, **kwargs):
            raise OSError("offline")
        monkeypatch.setattr(acceptance, "_api", unavailable)
        metadata["pr_left_open"] = {"reason": "Explicit exception does not waive read errors"}
    with connect() as conn:
        tid = kb.create_task(conn, title="No bypass", completion_contract="local-only" if mode == "local_publication" else PR)
    output = complete(surface, tid, metadata, monkeypatch)
    with connect() as conn:
        assert kb.get_task(conn, tid).status != "done", output
    assert ("infra" if mode == "read_error" else "landing_owner") in output


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("fault", ["failure", "pending", "missing", "stale", "no_required"])
def test_left_open_exception_preserves_ci_gate(github, monkeypatch, surface, fault):
    github["pr_state"] = "OPEN"
    if fault in {"failure", "pending"}:
        github["conclusion"] = fault
    else:
        github[fault] = True
    metadata = {"pr_left_open": {"reason": "Intentional draft"}}
    if fault == "no_required":
        github["no_ci"] = True
        metadata["contract_waiver"] = {"reason": "Local tests passed; no repo checks configured"}
    with connect() as conn:
        tid = kb.create_task(conn, title="CI still applies", completion_contract=PR)
    output = complete(surface, tid, metadata, monkeypatch)
    with connect() as conn:
        assert (kb.get_task(conn, tid).status == "done") is (fault == "no_required"), output


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_owner_on_other_board_is_not_a_landing_owner(github, monkeypatch, tmp_path, surface):
    github["pr_state"] = "OPEN"
    other = tmp_path / "other-board.db"
    kb.init_db(other)
    with connect(other) as conn:
        owner = kb.create_task(conn, title="Foreign landing", completion_contract=PR)
    with connect() as conn:
        tid = kb.create_task(conn, title="Implementation", completion_contract=PR)
    output = complete(surface, tid, {"landing_owner": owner}, monkeypatch)
    with connect() as conn:
        assert kb.get_task(conn, tid).status != "done", output
    assert "landing_owner" in output



