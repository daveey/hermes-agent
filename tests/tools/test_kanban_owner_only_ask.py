"""An owner-only terminal step is a human ask, not an agent relay (hub t_01a1c0d75a11b3cd).

run_in_user_terminal refuses a Kanban caller ("ask from your own Hermes chat"), cross_session refuses the
send, and kanban_block then rejected that same ask as agent_relay_ask. When both refusals are quoted as
evidence on an interaction=user_terminal ask there is no agent route left, so the ask must stand. A true
agent relay (no evidence of both refusals, or another interaction) is still refused.
"""
import json

import pytest

from tests.tools import test_kanban_agent_relay_ask as helpers

RELAY_81905 = helpers.RELAY_81905
_status = helpers._status
worker_env = helpers.worker_env

OWNER_ACTION = ("From your own Hermes chat open the owner-terminal flow for the exact dropped workflow. "
                "Unblock = verify cancellation; archive = abandon reconciliation.")
BOTH_REFUSALS = ("run_in_user_terminal refused: Kanban caller, owner chats only. "
                 "cross_session send refused: Kanban worker cannot send into a human chat.")
OWNER_ONLY = {**RELAY_81905, "interaction": "user_terminal", "action": OWNER_ACTION, "evidence": BOTH_REFUSALS,
              "why_human": "host_only boundary: only the owner's Desktop chat may open a terminal"}


def test_owner_only_ask_with_both_refusals_passes_lint():
    from hermes_cli.kanban_block_policy import owner_only_action_evidenced, user_ask_lint_error
    assert owner_only_action_evidenced(OWNER_ONLY)
    assert user_ask_lint_error(OWNER_ONLY) is None


@pytest.mark.parametrize("override", [
    {"evidence": "run_in_user_terminal refused for a Kanban caller"},         # cross_session not tried
    {"evidence": "cross_session send refused for a Kanban caller"},           # terminal not tried
    {"evidence": "capability routing returned no_capability_holder"},         # true agent relay
    {"evidence": ""},
    {"interaction": "external"},                                              # not a terminal step
    {"interaction": "approval"},
    {"action": "Ask your agent to repair the secret. Unblock = verify; archive = abandon."},
    {"action": "From your own Hermes chat apply an amendment. Unblock = verify; archive = abandon."},
    {"action": OWNER_ACTION + " Ask your agent to repair the secret."},
    {"why_human": "Another gateway has AWS credentials"},
    {"evidence": "run_in_user_terminal was not refused; cross_session was not refused"},
    {"evidence": "I did not try run_in_user_terminal or cross_session because they would be refused"},
    {"evidence": "run_in_user_terminal worked fine but cross_session refused"},
    {"evidence": "run_in_user_terminal would be refused. cross_session send would be refused."},
    {"evidence": "run_in_user_terminal refused: network down. cross_session send refused: network down."},
])
def test_relay_ask_without_both_refusals_is_still_refused(override):
    from hermes_cli.kanban_block_policy import user_ask_lint_error
    err = user_ask_lint_error({**OWNER_ONLY, **override})
    assert err and err.startswith("agent_relay_ask"), err


def test_non_dict_is_not_owner_only():
    from hermes_cli.kanban_block_policy import owner_only_action_evidenced
    assert not owner_only_action_evidenced(None)
    assert not owner_only_action_evidenced("run_in_user_terminal refused cross_session refused")


def test_block_entry_accepts_evidenced_owner_only_ask(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "owner-only terminal step", "kind": "capability",
                                     "human_request": OWNER_ONLY}))
    assert d.get("ok") is True, d
    assert _status(worker_env) == "blocked"


def test_block_entry_still_refuses_true_relay(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "x", "kind": "capability",
                                     "human_request": {**OWNER_ONLY, "evidence": "no_capability_holder"}}))
    assert d.get("rejection") == "agent_relay_ask", d
    assert _status(worker_env) == "running"


def test_standing_ask_sweep_keeps_evidenced_owner_only_ask():
    from hermes_cli.kanban_block_policy import standing_ask_error
    assert standing_ask_error(None, "t_x", "title", OWNER_ONLY) is None
    err = standing_ask_error(None, "t_x", "title", {**OWNER_ONLY, "evidence": "none"})
    assert err and err[0] == "agent_relay_ask"
