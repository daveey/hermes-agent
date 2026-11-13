"""When did a human last type a prompt into a Desktop chat on this host? [daveey]

hermes-sync opens a core-update drain window (admission held for up to 15 min, so a new message waits in
its backend's queue) only while the user is away. The MacBook's HID idle time misses a user typing into
a MacBook-hosted chat from ANOTHER Mac's Desktop (desktop-ssh: that client's keyboard is not this Mac's),
so every serve backend also records the time of each human-typed ``prompt.submit`` here, local and
desktop-ssh clients alike, in ``<HERMES_HOME>/desktop/last_human_input.json`` (atomic replace).

Only client-sent submits count. Injected turns never reach the ``prompt.submit`` RPC handler (Kanban
notices and wakes, auto-continue resumes and queued drains call ``_run_prompt_submit`` directly), and the
handler skips the in-process ones that do: bot-relay / hosted-room submits (``_turn_author`` /
``_hosted_*``), ``start_chat`` (no request id), hidden widget intents, and cross-session sends (the
session-mentions courier submits over the websocket, with its fixed header first). Agent scripts that dial
``/api/ws?client=agent`` (gateway_rpc.py) never stamp either, nor do submits into chats they created.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_REL = Path("desktop") / "last_human_input.json"
# session-mentions (hermes-mods) prefixes every cross-session delivery with this header.
CROSS_SESSION_HEADER = "[Message from another Hermes session"
_INTERNAL_PARAMS = ("_turn_author", "_hosted_task", "_hosted_terminal_callback")


def is_human_typed_submit(rid, params: dict, text) -> bool:
    """True for a ``prompt.submit`` a person typed in a client (not an injected or relayed turn)."""
    if rid is None or not isinstance(params, dict):
        return False
    from tui_gateway.transport import current_transport, is_agent_transport

    if is_agent_transport(current_transport()):  # an agent script over /api/ws?client=agent, not a person typing
        return False
    if any(params.get(k) is not None for k in _INTERNAL_PARAMS):
        return False
    if params.get("display_kind") == "hidden":
        return False
    if not isinstance(text, str) or not text.strip():
        return False
    return not text.lstrip().startswith(CROSS_SESSION_HEADER)


def state_path(home: Path | None = None) -> Path:
    if home is None:
        import hermes_constants

        home = Path(hermes_constants.get_hermes_home())
    return Path(home) / STATE_REL


def record_human_input(session_id: str = "", home: Path | None = None) -> None:
    """Atomically stamp now as the last human-typed prompt. Never raises."""
    try:
        path = state_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"at": time.time(), "session_id": str(session_id or ""), "pid": os.getpid()}))
        os.replace(tmp, path)
    except Exception:  # health: allow BLE001 -- a stamp must never fail a prompt
        logger.debug("could not record the last human input", exc_info=True)


def input_origin(rid, params, text, *, transport=None, session: dict | None = None) -> str:
    """Who wrote this client ``prompt.submit``: ``"agent"``, ``"human"`` or ``""`` (unknown). Never raises.

    ``"agent"``: a websocket opened with ``/api/ws?client=agent`` (*transport*, default the bound one), a session
    such a connection created or a hidden (bot plumbing) one (*session*), a relayed/hosted turn, a hidden widget
    intent, or a cross-session send. ``"human"``: :func:`is_human_typed_submit`. ``pre_llm_call`` plugins read it
    as ``input_origin`` (agent/turn_context.py); injected turns never reach here and stay ``""``."""
    try:
        from tui_gateway.transport import current_transport, is_agent_transport

        if is_agent_transport(current_transport() if transport is None else transport):
            return "agent"
        if isinstance(session, dict) and (session.get("agent_created") or session.get("pending_hidden")):
            return "agent"
        if rid is None or not isinstance(params, dict):
            return ""
        if any(params.get(k) is not None for k in _INTERNAL_PARAMS) or params.get("display_kind") == "hidden":
            return "agent"
        if isinstance(text, str) and text.lstrip().startswith(CROSS_SESSION_HEADER):
            return "agent"
        return "human" if is_human_typed_submit(rid, params, text) else ""
    except Exception:  # health: allow BLE001 -- a classification must never fail a prompt
        logger.debug("input-origin check failed", exc_info=True)
        return ""


def note_submit(rid, params: dict, text, *, transport=None, session: dict | None = None) -> str:
    """Record ``prompt.submit`` when a human typed it; return its :func:`input_origin`. Never raises.

    Agent traffic never stamps: a submit over a websocket opened with ``/api/ws?client=agent`` (*transport*,
    default the bound one), or into a session such a connection created (*session* ``agent_created``)."""
    origin = input_origin(rid, params, text, transport=transport, session=session)
    if origin == "human":
        record_human_input(str(params.get("session_id") or ""))
    return origin
