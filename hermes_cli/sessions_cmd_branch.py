"""``hermes sessions branch <id>``: copy a chat into a hidden session for probes and tests. [daveey]

``/branch`` is interactive-only, so an agent that had to prove "a turn on chat X works" resumed the person's real
chat with a one-shot query; its test prompt and token reply became the newest turns the user saw (self-heal
agent-93a55211b1). This command makes the copy in one call and prints its id. The copy carries the parent's
transcript (same rows and fields ``/branch`` copies, see :data:`BRANCH_COPY_KEYS`) and system prompt, but is a root
row with ``source='oneshot'`` (an internal listing source) and a ``_probe_copy_of`` marker, so it never appears in
the user's sidebar or ``/resume`` picker and the user-chat-probe-guard lets probes into it. The original is not
touched (not ended, no child row). Delete the copy afterwards with ``hermes sessions delete --yes <copy>``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

# Message fields copied verbatim onto a branch row (plus role / tool_name / api_content / identity). Shared with
# the interactive /branch (hermes_cli.cli_commands_mixin) so both copies carry the same columns.
BRANCH_COPY_KEYS = ("content", "tool_calls", "tool_call_id", "reasoning", "reasoning_content", "reasoning_details",
                    "codex_reasoning_items", "codex_message_items", "timestamp", "data_use")


def branch_rows(history: list) -> list:
    """Rows for ``append_messages_batch`` from an OpenAI-format history (``get_messages_as_conversation``)."""
    from agent.message_metadata import message_identity
    from agent.turn_context import extract_api_content_sidecar

    return [{"role": msg.get("role", "user"), "tool_name": msg.get("tool_name") or msg.get("name"),
             "api_content": extract_api_content_sidecar(msg), **{k: msg.get(k) for k in BRANCH_COPY_KEYS},
             **message_identity(msg, with_tool_uids=True)} for msg in history]


def cmd_branch(db, args) -> int:
    parent = db.resolve_session_id(args.session_id)
    if not parent:
        print(f"Session '{args.session_id}' not found.")
        return 1
    read_id = db.resolve_resume_session_id(parent) or parent  # the compression tip holding the messages
    history = [m for m in db.get_messages_as_conversation(read_id) if m.get("role") != "session_meta"]
    meta = db.get_session(read_id) or db.get_session(parent) or {}
    from hermes_state_ids import new_session_id

    new_id = new_session_id(datetime.now())
    title = args.title or f"probe copy of {parent}"
    db.create_session(new_id, source=args.source or "oneshot", model=meta.get("model"),
                      system_prompt=meta.get("system_prompt") or None, cwd=meta.get("cwd"),
                      model_config={"_probe_copy_of": parent})
    try:
        if history:
            db.append_messages_batch(new_id, branch_rows(history), chunk_rows=500)
        db.set_session_title(new_id, title)
    except Exception:
        # A durable-but-empty copy would answer the probe with no context: remove it, then fail loudly.
        try:
            db.delete_session(new_id)
        except Exception:  # health: allow BLE001 -- best-effort cleanup; the original error is re-raised below
            logger.warning("sessions branch: could not remove the partial copy %s", new_id, exc_info=True)
        raise
    cleanup = f"hermes sessions delete --yes {new_id}"
    if args.json:
        print(json.dumps({"session_id": new_id, "copied_from": parent, "read_from": read_id,
                          "messages": len(history), "source": args.source or "oneshot", "cleanup": cleanup}))
    else:
        print(new_id)
        print(f"Copied {len(history)} message(s) from {parent}. Probe it with: hermes chat --resume {new_id} "
              f"--oneshot -q '...'; then: {cleanup}", file=sys.stderr)
    return 0
