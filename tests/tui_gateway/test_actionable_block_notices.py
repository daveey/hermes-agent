"""Exercise all block notice entry paths with the same current card evidence."""
from types import SimpleNamespace

import pytest

ASK = {"action": "Authorize the reviewed activation in your Desktop session. " + "Keep the safety gate. " * 12 + "Unblock = verify; archive = abandon.",
       "why_human": "Only the owner session holds terminal authority.", "interaction": "user_terminal",
       "scope": "One reviewed activation", "evidence": "Owner boundary refusal"}


def render(path, card):
    ev = SimpleNamespace(kind="blocked", payload={"reason": "Opaque identity boundary"})
    if path == "remote":
        from tui_gateway.kanban_remote_notices import format_notice
        return format_notice("remote-host", "ops/t_example", card)
    task = SimpleNamespace(**card)
    if path == "gateway":
        from gateway.kanban_watchers_notifier import _fmt_blocked
        return _fmt_blocked(ev, SimpleNamespace(task=task, task_id=card["id"], board_slug="ops", head="[ops] @worker Kanban t_example"))[0]
    from tui_gateway import server
    return server._format_kanban_event_text({"task_id": card["id"]}, task, ev, "ops")


@pytest.mark.parametrize("path", ["local", "remote", "gateway"])
@pytest.mark.parametrize("case", ["human", "capability", "missing", "resolved"])
def test_actionable_block_entry_paths(path, case):
    card = dict(id="t_example", title="Activation", status="blocked", assignee="worker", block_kind="needs_input", human_request=dict(ASK))
    if case == "capability":
        card.update(block_kind="capability", human_request=None)
    elif case == "missing":
        card["human_request"] = {"action": "Ask the user"}
    elif case == "resolved":
        card["status"] = "ready"
    text = render(path, card)
    assert "t_example blocked" in text or "t_example Earlier" in text
    assert f"@task:ops{'@remote-host' if path == 'remote' else ''}/t_example" in text
    if case == "human":
        assert ASK["action"] in text
        assert ASK["why_human"] in text
        assert "actor: user" in text
        assert "does not execute" in text
    elif case == "resolved":
        assert "Earlier block resolved" in text
        assert "actor: user" not in text
    else:
        assert "actor: agent" in text
        assert "Next action: the agent" in text


def test_outcome_rejects_legacy_human_wait_without_evidence(monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    import hermes_state_outcome as outcome
    monkeypatch.setattr(activity, "session_tasks", lambda *a, **k: [dict(id="t_example", status="blocked", wait={"wait_state": "human"})])
    decisions, waits = outcome._card_items(["chat"])
    assert decisions == []
    assert waits[0]["id"] == "t_example"


def test_stored_json_request_and_wake_carry_full_ask():
    import json
    from gateway.kanban_watchers_notifier import _fmt_blocked
    task = SimpleNamespace(id="t_example", status="blocked", assignee="worker", block_kind="needs_input",
                           human_request=json.dumps(ASK))
    ev = SimpleNamespace(kind="blocked", payload={"reason": "Opaque identity boundary"})
    msg, wake, _ = _fmt_blocked(ev, SimpleNamespace(task=task, task_id="t_example", board_slug="ops", head="Kanban t_example"))
    assert ASK["action"] in msg and ASK["action"] in wake
    assert "Opaque identity boundary" not in msg


def test_deleted_card_is_agent_owned():
    from gateway.kanban_watchers_notifier import _fmt_blocked
    ev = SimpleNamespace(kind="blocked", payload={"reason": "gone"})
    msg = _fmt_blocked(ev, SimpleNamespace(task=None, task_id="t_example", board_slug="ops", head="Kanban t_example"))[0]
    assert "actor: agent" in msg and "@task:ops/t_example" in msg


def test_outcome_decision_keeps_full_validated_action(monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    import hermes_state_outcome as outcome
    monkeypatch.setattr(activity, "session_tasks", lambda *a, **k: [dict(
        id="t_example", status="blocked", block_kind="needs_input", human_request=dict(ASK))])
    decisions, waits = outcome._card_items(["chat"])
    assert waits == []
    assert decisions[0]["question"] == ASK["action"]
