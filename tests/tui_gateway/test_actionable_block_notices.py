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
    monkeypatch.setattr(activity, "session_outcome_tasks", lambda *a, **k: [dict(id="t_example", status="blocked", wait={"wait_state": "human"})])
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
    monkeypatch.setattr(activity, "session_outcome_tasks", lambda *a, **k: [dict(
        id="t_example", status="blocked", block_kind="needs_input", human_request=dict(ASK))])
    decisions, waits = outcome._card_items(["chat"])
    assert waits == []
    assert decisions[0]["question"] == ASK["action"]


@pytest.mark.parametrize("actor,chat,match", [
    ("agent (chat agent:main:local:dm:20261026_235722_bef6c6)", "20261026_235722_bef6c6", True),
    ("agent (chat 20261026_235722_bef6c6)", "20261026_235722_bef6c6", True),
    ("agent (chat agent:writer:telegram:dm:chat-1)", "chat-1", True),
    ("agent (chat agent:main:local:dm:20261026_235722_bef6c6)", "20261026_235722_other1", False),
    ("agent (chat agent:main:telegram:group:chat-1:u9)", "u9", False),
    ("agent (chat agent:main:telegram:group:chat-1)", "chat-1", False),
    ("agent (task t_1)", "t_1", False),
    ("user", "user", False),
    ("agent (chat x)", "", False),
    (None, "x", False),
])
def test_actor_chat_matches_bare_sub_chat_id(actor, chat, match):
    """(d) The actor's gateway DM session key matches the bare chat id a subscription stores."""
    from hermes_cli.kanban_block_notice import actor_is_chat
    assert actor_is_chat(actor, chat) is match


AMEND = {"metadata_only": True, "classified_in_place": True, "kind": "needs_input", "reason": "Opaque identity boundary",
         "actor": "agent (chat agent:main:local:dm:20261026_235722_bef6c6)", "human_request": dict(ASK)}


def render_amend(path, card, chat_id):
    ev = SimpleNamespace(kind="blocked", payload=dict(AMEND))
    task = SimpleNamespace(**card)
    if path == "gateway":
        from gateway.kanban_watchers_notifier import _fmt_blocked
        return _fmt_blocked(ev, SimpleNamespace(task=task, task_id=card["id"], board_slug="ops", head="Kanban t_example",
                                                sub={"chat_id": chat_id}))[0]
    from tui_gateway import server
    return server._format_kanban_event_text({"task_id": card["id"], "chat_id": chat_id}, task, ev, "ops")


@pytest.mark.parametrize("path", ["local", "gateway"])
def test_metadata_only_amend_entry_paths(path):
    """(a) silent for the chat that amended the ask; (b) one "ask updated" line for any other subscriber."""
    card = dict(id="t_example", title="Activation", status="blocked", assignee="worker", block_kind="needs_input",
                human_request=dict(ASK))
    assert render_amend(path, card, "20261026_235722_bef6c6") is None
    text = render_amend(path, card, "other-chat")
    assert text.endswith(f"Kanban t_example ask updated (card still blocked): {ASK['action']} — open task: @task:ops/t_example")
    assert "blocked —" not in text and len(text.splitlines()) == 1
    # A card unblocked before delivery reports its current state instead.
    assert "Earlier block resolved; current state: ready." in render_amend(path, {**card, "status": "ready"}, "other-chat")


@pytest.mark.parametrize("path", ["local", "gateway"])
def test_real_block_by_the_same_chat_is_unchanged(path):
    """(c) Current policy: a real block notifies its own actor chat with the full notice."""
    from tui_gateway import server
    from gateway.kanban_watchers_notifier import _fmt_blocked
    card = dict(id="t_example", title="Activation", status="blocked", assignee="worker", block_kind="needs_input",
                human_request=dict(ASK))
    payload = {k: v for k, v in AMEND.items() if k not in ("metadata_only", "classified_in_place")}
    ev, task, chat = SimpleNamespace(kind="blocked", payload=payload), SimpleNamespace(**card), "20261026_235722_bef6c6"
    text = (_fmt_blocked(ev, SimpleNamespace(task=task, task_id="t_example", board_slug="ops", head="Kanban t_example",
                                             sub={"chat_id": chat}))[0] if path == "gateway" else
            server._format_kanban_event_text({"task_id": "t_example", "chat_id": chat}, task, ev, "ops"))
    assert f"t_example blocked — actor: {AMEND['actor']}" in text
    assert f"Next action for the user: {ASK['action']}" in text
