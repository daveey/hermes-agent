"""``session.create``'s born-hidden default for agent clients. [daveey]

Agents that drive the Desktop backend over ``/api/ws?client=agent`` (proof cards, the patching-local-hermes
``gateway_rpc.py``) mint throwaway chats; those are born hidden so they never land in the user's sidebar.
Hidden only changes the default list: the chat still runs, auto-continues and receives kanban notices.
The marker is client-declared, so this picks a default and is not a security boundary.
"""

from __future__ import annotations

from tui_gateway.transport import is_agent_client
from utils import is_truthy_value


def create_hidden(params: dict) -> bool:
    """The ``pending_hidden`` intent for a new session: an explicit ``hidden`` always wins (``false``
    included); without one, a session created over an agent-declared transport is born hidden."""
    if params.get("hidden") is not None:
        return is_truthy_value(params["hidden"])
    return is_agent_client()
