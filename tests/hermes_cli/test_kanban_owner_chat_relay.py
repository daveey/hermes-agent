"""Historical owner-chat asks must not bypass the relay presentation lint."""
import pytest

from hermes_cli.kanban_block_policy import agent_relay_ask_error


# Exact action bytes from hub task_events 1145465 and 1152154, respectively.
HISTORICAL_ACTIONS = (
    "From owner chat 20261111_032644_40d5be, run the single ssh command in this card's unblock steps through run_in_user_terminal; it runs the isolated 24-test native check on the fixed Ask Eve app (about 90 s, nothing live changes). Unblock = the run finished, pass or fail, and the worker continues to the reviewed apply; archive = abandon the fix-forward and leave Ask Eve stopped.",
    "In owner Desktop chat 20261102_172718_5fb607, run the reviewed r10 check×2 then apply through run_in_user_terminal using comment194474. Unblock = worker verifies the result; archive = abandon this activation, leaving Ask Eve stopped.",
)


@pytest.mark.parametrize("action", HISTORICAL_ACTIONS)
def test_historical_owner_chat_actions_are_rejected(action):
    error = agent_relay_ask_error(action)
    assert error is not None and error.startswith("agent_relay_ask:")


@pytest.mark.parametrize("article", ["", "a ", "an ", "any ", "the "])
@pytest.mark.parametrize("owner", ["owner", "owner's"])
@pytest.mark.parametrize("surface", ["chat", "Desktop chat", "Hermes Desktop or CLI session"])
def test_owner_chat_article_and_possessive_forms(article, owner, surface):
    assert agent_relay_ask_error(f"From {article}{owner} {surface}, run the check.")


@pytest.mark.parametrize("action", [
    "Approve the owner rollout",
    "Run the check in owner rollout documentation.",
    "Ask the vendor's agent to approve the owner rollout.",
])
def test_unrelated_owner_language_stays_allowed(action):
    assert agent_relay_ask_error(action) is None


@pytest.mark.parametrize("verb", ["open", "start"])
def test_canonical_owner_terminal_template_stays_allowed(verb):
    action = (
        f"From your own Hermes chat {verb} the owner-terminal flow for task t_0123456789abcdef. "
        "Unblock = resume verification; archive = abandon reconciliation."
    )
    request = {
        "action": action,
        "interaction": "user_terminal",
        "why_human": "host_only boundary: only the owner's Desktop chat may open a terminal",
        "evidence": (
            "run_in_user_terminal refused: Kanban caller, owner chats only. "
            "cross_session send refused: Kanban worker cannot send into a human chat."
        ),
    }
    assert agent_relay_ask_error(action, request) is None
    assert agent_relay_ask_error(action)  # The attestation is still mandatory.
