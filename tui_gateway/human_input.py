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
session-mentions courier submits over the websocket, with its fixed header first).
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


def note_submit(rid, params: dict, text) -> None:
    """Record ``prompt.submit`` when a human typed it. Never raises."""
    try:
        if is_human_typed_submit(rid, params, text):
            record_human_input(str(params.get("session_id") or ""))
    except Exception:  # health: allow BLE001 -- a stamp must never fail a prompt
        logger.debug("human-input check failed", exc_info=True)
