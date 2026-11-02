"""Landing disposition reaches origin release, every done-notice renderer, and the durable payload."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.test_kanban_pr_landing import PR, complete
pytest_plugins = ["tests.hermes_cli.test_kanban_pr_acceptance"]

CHAT = "20261102_060000_abc123"
REASON = "User asked to keep the draft for later evaluation"
CAVEAT = "hub batch only installs merged main; it never merges PRs"


def _done(github, state, metadata, summary="landed", contract=PR):
    github["pr_state"] = state
    with connect() as conn:
        tid = kb.create_task(conn, title="Landing", completion_contract=contract)
        owner = kb.create_task(conn, title="Owner", completion_contract=PR)
        metadata = {k: (owner if v == "<owner>" else v) for k, v in metadata.items()}
        assert kb.complete_task(conn, tid, summary=summary, metadata=metadata)
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0])
    return tid, owner, payload


def _observe(tid):
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    with connect() as conn:
        conn.execute("BEGIN")
        try:
            return observe_owner_task(conn, {"id": tid, "status": "done", "origin_bindings": []}, now=0)
        finally:
            conn.rollback()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("state,metadata", [
    ("MERGED", {}), ("CLOSED", {}),
    ("OPEN", {"pr_left_open": {"reason": REASON}}), ("OPEN", {"landing_owner": "<owner>"})],
    ids=["merged", "closed", "open_left_open", "open_landing_owner"])
def test_every_accepted_disposition_releases_origin(github, state, metadata):
    tid, _owner, _payload = _done(github, state, metadata)
    assert _observe(tid)["origin_observation"]["verified"] is True


@pytest.mark.platforms("linux")
def test_closed_receipt_on_other_pr_does_not_release_origin(github):
    tid, _owner, _payload = _done(github, "CLOSED", {})
    with connect() as conn:
        conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (PR + "0", tid))
        conn.commit()
    assert _observe(tid)["origin_observation"]["verified"] is False


def _left_open_payload(github, summary="handoff"):
    return _done(github, "OPEN", {"pr_left_open": {"reason": REASON}}, summary=summary)


def _assert_notice(text):
    assert REASON in text and CAVEAT in text, text


@pytest.mark.platforms("linux")
def test_left_open_in_local_gateway_notice(github):
    from gateway.kanban_watchers_notifier import _fmt_completed
    tid, _owner, payload = _left_open_payload(github)
    with connect() as conn:
        task = kb.get_task(conn, tid)
    notice, wake, _ = _fmt_completed(SimpleNamespace(payload=payload), SimpleNamespace(task=task, head="Done", title="L"))
    _assert_notice(notice)
    _assert_notice(wake)


@pytest.mark.platforms("linux")
def test_left_open_in_tui_notice(github):
    from tui_gateway.session_notifications import _format_kanban_event_text
    tid, _owner, payload = _left_open_payload(github)
    ev = SimpleNamespace(kind="completed", payload=payload)
    _assert_notice(_format_kanban_event_text({"task_id": tid, "chat_id": CHAT}, SimpleNamespace(title="L"), ev, "default"))


@pytest.mark.platforms("linux")
def test_left_open_in_tui_final_send_notice(github):
    from tui_gateway.session_notifications import _format_kanban_event_text
    tid, _owner, payload = _left_open_payload(github, f"prose\nFINAL SEND for @session:default/{CHAT}: final proof")
    ev = SimpleNamespace(kind="completed", payload=payload)
    text = _format_kanban_event_text({"task_id": tid, "chat_id": CHAT}, SimpleNamespace(title="L"), ev, "default")
    assert "final proof" in text
    _assert_notice(text)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("final", [False, True])
def test_left_open_in_remote_notice(github, final):
    from tui_gateway.kanban_remote_notices import _completion_text
    summary = f"prose\nFINAL SEND for @session:default@macbook/{CHAT}: remote proof" if final else "handoff"
    tid, _owner, _payload = _left_open_payload(github, summary)
    published = _observe(tid)
    task = {**published, "title": "L", "board": "default"}
    text = _completion_text("hub", f"default/default/{tid}", task, "macbook", "default", CHAT)
    assert ("remote proof" in text) is final
    _assert_notice(text)


@pytest.mark.platforms("linux")
def test_merged_payload_ignores_raw_landing_metadata(github):
    from gateway.kanban_watchers_notifier import _fmt_completed
    from tui_gateway.session_notifications import _format_kanban_event_text
    tid, _owner, payload = _done(github, "MERGED", {
        "pr_left_open": {"reason": REASON}, "landing_owner": "anything; not a card"})
    assert "pr_left_open" not in payload and "landing_owner" not in payload
    with connect() as conn:
        task = kb.get_task(conn, tid)
    notice, _wake, _ = _fmt_completed(SimpleNamespace(payload=payload), SimpleNamespace(task=task, head="Done", title="L"))
    tui = _format_kanban_event_text({"task_id": tid, "chat_id": CHAT}, SimpleNamespace(title="L"),
                                    SimpleNamespace(kind="completed", payload=payload), "default")
    for text in (notice, tui):
        assert "left open" not in text


@pytest.mark.platforms("linux")
def test_local_only_payload_ignores_raw_landing_metadata(github):
    _tid, _owner, payload = _done(github, "OPEN", {"pr_left_open": {"reason": REASON}, "landing_owner": "x"},
                                  contract="local-only")
    assert "pr_left_open" not in payload and "landing_owner" not in payload


@pytest.mark.platforms("linux")
def test_open_payload_records_validated_landing_owner(github):
    _tid, owner, payload = _done(github, "OPEN", {"landing_owner": "<owner>"})
    assert payload["landing_owner"] == owner
    assert "pr_left_open" not in payload


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("value", [7, [PR], "not a pull request URL"], ids=["int", "list", "string"])
@pytest.mark.parametrize("contract", ["local-only", "daveey/hermes"])
def test_malformed_published_pr_is_its_own_refusal(github, monkeypatch, surface, value, contract):
    from hermes_cli import kanban_pr_acceptance as acceptance
    def no_network(*args, **kwargs):
        raise AssertionError("malformed publication must not read GitHub")
    monkeypatch.setattr(acceptance, "_api", no_network)
    with connect() as conn:
        tid = kb.create_task(conn, title="Local", completion_contract=contract)
    output = complete(surface, tid, {"published_pr": value}, monkeypatch)
    with connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status != "done", output
        assert task.completion_contract == contract
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "malformed_published_pr"
    assert "malformed_published_pr" in output
    assert "infra" not in output and "persisted completion contract" not in output


def test_guidance_steers_review_only_cards_away_from_published_pr():
    from agent.prompt_builder import KANBAN_GUIDANCE
    from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA
    for text in (KANBAN_GUIDANCE, json.dumps(KANBAN_COMPLETE_SCHEMA)):
        assert "metadata.reviewed_pr" in text and "metadata.pr" in text
        assert "review-only" in text.lower()
