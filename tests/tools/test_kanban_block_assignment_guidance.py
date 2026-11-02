"""Block refusals offer kind='assignment' only where it can route.

Hub and MacBook leave ``kanban.orchestrator_profile`` unset, and their cards are created and run by the same
``default`` profile, so ``kanban_block(kind='assignment')`` always fails with ``no_capable_supervisor``
(``_supervisor`` excludes the assignee). In 72 h, 38 cards hit it (25 hub, 13 MacBook) after the refusal text and
the schema told them to use it. The workers then improvised: asks to David for agent work, self-parks with no
producer. The route stays profile-only (a same-profile worker cannot grant lifecycle authority), so the text now
offers assignment per card, only when a different capable profile exists, and otherwise lists the routes that
work on one profile: an owner card plus a card park, ``needs=[...]``, and ``kanban_supervisor_request``. [daveey]
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

PROFILE_CFG = "platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n"
ALTERNATIVES = ("kanban_create", "needs=", "kanban_supervisor_request")


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (h / "config.yaml").write_text("kanban:\n  orchestrator_profile: ''\n")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _with_supervisor(home):
    """orchestrator_profile names an existing, different profile with the card's tools plus kanban."""
    (home / "config.yaml").write_text("kanban:\n  orchestrator_profile: supervisor\n")
    prof = home / "profiles" / "supervisor"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text(PROFILE_CFG)


def _card(*, claim):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs a grant", assignee="default", created_by="default")
        if claim:
            kb.claim_task(conn, tid)
            return tid, kb._current_run_id(conn, tid)
    return tid, None


def _tool_refusal(monkeypatch):
    from tools import kanban_tools as kt
    tid, run_id = _card(claim=True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    d = json.loads(kt._handle_block({"kind": "capability", "reason": "need a grant"}))
    assert d.get("rejection") == "agent_owned_block", d
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"  # still refused, card untouched
    return d["error"]


def _cli_refusal():
    tid, _ = _card(claim=False)
    out = kanban_cli.run_slash(f"block {tid} need a grant")
    assert "agent_owned_block" in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
    return out


def _assert_single_profile_routes(text):
    assert "kind='assignment'" not in text, text
    for word in ALTERNATIVES:
        assert word in text, (word, text)
    assert "awaiting={kind:'card'" in text
    for op in ("archive", "unlink", "resume", "retry_limit"):
        assert op in text, op
    assert "human_request" in text  # David-only steps keep their route


# --- agent_owned_block refusal: tool + CLI ------------------------------------------------------

def test_tool_refusal_on_single_profile_host_omits_assignment(home, monkeypatch):
    _assert_single_profile_routes(_tool_refusal(monkeypatch))


def test_cli_refusal_on_single_profile_host_omits_assignment(home):
    _assert_single_profile_routes(_cli_refusal())


def test_tool_refusal_offers_assignment_when_a_different_profile_is_configured(home, monkeypatch):
    _with_supervisor(home)
    assert "kind='assignment'" in _tool_refusal(monkeypatch)


def test_cli_refusal_offers_assignment_when_a_different_profile_is_configured(home):
    _with_supervisor(home)
    assert "kind='assignment'" in _cli_refusal()


def test_refusal_without_a_task_never_offers_assignment():
    from hermes_cli.kanban_block_policy import block_policy_error
    _assert_single_profile_routes(block_policy_error("capability", reason="need a grant"))


def test_supervisor_available_matches_route_eligibility(home):
    from hermes_cli import kanban_assignment_routing as ar
    tid, _ = _card(claim=False)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert ar.supervisor_available(task) is False
    _with_supervisor(home)
    assert ar.supervisor_available(task) is True


# --- route(): no_capable_supervisor --------------------------------------------------------------

def test_no_capable_supervisor_refusal_names_the_working_routes(home):
    from hermes_cli import kanban_assignment_routing as ar
    tid, _ = _card(claim=False)
    with kbc.connect_closing() as conn:
        with pytest.raises(ar.AssignmentRoutingError) as exc:
            kb.block_task(conn, tid, kind="assignment", reason="need a grant")
        assert kb.get_task(conn, tid).status == "ready"
    text = str(exc.value)
    assert text.startswith("no_capable_supervisor")
    assert "configure kanban.orchestrator_profile" not in text
    assert "assignment cannot help" in text
    for word in ALTERNATIVES:
        assert word in text, word


# --- schema text -----------------------------------------------------------------------------------

def test_block_schema_no_longer_prescribes_assignment_for_agent_work():
    from tools.kanban_tools_schemas import KANBAN_BLOCK_SCHEMA
    desc = KANBAN_BLOCK_SCHEMA["description"]
    kind_desc = KANBAN_BLOCK_SCHEMA["parameters"]["properties"]["kind"]["description"]
    for text in (desc, kind_desc):
        assert "agent work is 'assignment'" not in text
        assert "no_capable_supervisor" in text or "different profile" in text
    assert "route to a capable supervisor, not a human" not in desc
    assert "kanban_supervisor_request" in kind_desc
    assert "single-profile" in desc


def test_worker_prompt_and_awaiting_remedy_qualify_assignment():
    from agent import prompt_builder
    from hermes_cli import kanban_awaiting
    assert 'holds is `kind="assignment"` or `needs=[...]`' not in prompt_builder.KANBAN_GUIDANCE
    assert "only when a different profile is configured" in prompt_builder.KANBAN_GUIDANCE
    remedy = kanban_awaiting._REMEDY
    assert "kind='assignment' so a capable supervisor is routed" not in remedy
    assert "needs=" in remedy and "kanban_supervisor_request" in remedy
