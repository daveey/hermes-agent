"""Per-turn model-input notes split from session_notifications: the HUD/voice surface note and the prefixer that puts
any note on the model input. Bodies are rebound onto server.py's globals (method_ctx.bind_module)."""

from __future__ import annotations

from typing import Any

from .method_ctx import bind_module


def _hud_surface_note(session: dict) -> str:
    """The per-surface note for this turn ("" for the plain app window): HUD -> the read-the-window-below
    prior; voice-live -> the spoken-delegation contract with the recent transcript."""
    from gateway.session_surface import surface_note
    surface, agent = session.get("client_surface"), session.get("agent")
    direct = getattr(agent, "valid_tool_names", None) or set()
    if surface == "hud":
        from tools.tool_search_catalog import TOOL_CALL_NAME
        if TOOL_CALL_NAME in direct:
            # Tool search defers the desktop tools by default: they stay callable through the
            # tool_call bridge, so the HUD note must count them and name the bridge (main 312f6bc2fd6).
            from agent.prompt_builder import hud_surface_note
            from agent.tool_executor import _tool_search_scoped_names
            return hud_surface_note(direct, _tool_search_scoped_names(agent))
    committed = {"surface": surface, "voice_context": session.get("voice_live_context")}
    return surface_note(committed, direct)


def _prepend_note(run_message: Any, note: str) -> Any:
    """Prefix a per-turn note onto the MODEL INPUT, leaving the prompt alone: everything the model must know that the
    user did not type (interrupted reply, reactions, surface) arrives this way, so no scaffolding reaches the
    transcript and no sent message is rewritten — the cached prefix survives."""
    if note and isinstance(run_message, str):
        return f"{note}\n\n{run_message}"
    if note and isinstance(run_message, list):
        return [{"type": "text", "text": note}, *run_message]
    return run_message


def _loop_route_is_gateway_chat(state) -> bool:
    """A /loop set from a messaging chat carries the gateway's ``route`` (platform + chat_id); its wakeup scanner
    (``gateway/run_goals.py::_loop_wakeup_fire_one``) fires those and skips route-less CLI/TUI loops. Mirror it in
    session_notifications so a Desktop viewer of the same session never consumes the tick and strands the reply."""
    route = getattr(state, "route", None) or {}
    return bool(route.get("platform") and route.get("chat_id"))


def register(server) -> None:
    """Publish this module's helpers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
