"""Mid-run plugin activation: what a just-loaded plugin does NOW vs next session (#87770).

One seam for every surface: ``PluginManager.on_plugin_loaded`` fires with one
:func:`plugin_activation_summary` per loaded plugin. Gateway handlers (slash commands, transform hooks,
platform callbacks) are live as soon as the plugin loads; portable MCP servers and skills go live in
the open chats of the plugin's profile (:func:`load_and_go_live`; MCP tools are deferred behind
tool_search, so the cached prompt prefix is unchanged); Python tools and system-prompt sections stay
deferred to the next session (prompt-cache invariant, same as ``/skills install``).
:func:`activate_plugin_now` is what the install / enable / update surfaces call after a successful
config or tree change.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_GO_LIVE_LOCK = threading.Lock()

# Hooks the gateway consults per inbound/outbound message: live as soon as the registry holds them.
_GATEWAY_TRANSFORM_HOOKS = frozenset({
    "transform_llm_output", "transform_tool_result", "transform_terminal_output", "pre_gateway_dispatch",
    "gateway_platform_event", "pre_command",
})


def plugin_activation_summary(manager: Any, plugin_key: str) -> dict[str, Any]:
    """``{name, key, activated_now: {kind: [names]}, deferred: {kind: [names]}}`` for one loaded plugin,
    read from what it actually registered (ownership ledger + handler registries), not from what its
    manifest promises. Keys appear only when non-empty.

    ``activated_now``: ``gateway_commands`` (slash names), ``gateway_transforms`` / ``hooks`` (hook names),
    ``callbacks`` (platforms with a ``register_platform_handler`` factory / Slack action ids), ``locales``
    (``<lang>.<surface>`` language-pack layers).
    ``deferred``: ``tools`` (tool names; next session), ``prompt`` (section ids; next session),
    ``mcp_servers`` (the plugin's mcp.json server names exactly as registered; until ``mcp.reload``)."""
    loaded = manager._plugins.get(plugin_key)
    manifest = getattr(loaded, "manifest", None)
    name = getattr(manifest, "name", None) or plugin_key
    regs = [r for r in manager._ownership_ledger.get(plugin_key, []) if getattr(r, "active", True)]
    kinds: dict[str, list[str]] = {}
    for reg in regs:
        kinds.setdefault(reg.kind, []).append(str(reg.key))
    now: dict[str, list[str]] = {}
    if kinds.get("command"):
        now["gateway_commands"] = sorted(kinds["command"])
    hooks = set(kinds.get("hook", ()))
    if hooks & _GATEWAY_TRANSFORM_HOOKS:
        now["gateway_transforms"] = sorted(hooks & _GATEWAY_TRANSFORM_HOOKS)
    if hooks - _GATEWAY_TRANSFORM_HOOKS:
        now["hooks"] = sorted(hooks - _GATEWAY_TRANSFORM_HOOKS)
    callbacks = sorted(platform for platform, factories in manager._platform_handler_factories.items()
                       if any(plugin == name for _f, plugin in factories))
    callbacks += [f"slack:{a}" for a in kinds.get("slack_action_handler", ())]
    if callbacks:
        now["callbacks"] = callbacks
    if kinds.get("locale"):  # language-pack layers: the i18n caches reset on registration, so live at once
        now["locales"] = sorted(kinds["locale"])
    load_error = ""
    if getattr(manifest, "kind", None) == "model-provider":
        # providers/ discovery owns these and the forced sweep re-scans them: an updated tree serves new
        # sessions once it imported; a failed import leaves the previous version serving (#153300).
        from providers import plugin_load_error
        load_error = plugin_load_error(Path(getattr(manifest, "path", None) or name).name)
        if not load_error:
            now["provider"] = [name]
    deferred: dict[str, list[str]] = {}
    tools = sorted(set(kinds.get("tool", ())) | set(getattr(loaded, "tools_registered", None) or ())
                   | set(getattr(manifest, "provides_tools", None) or ()))
    if tools:
        deferred["tools"] = tools
    if kinds.get("system_prompt_section"):
        deferred["prompt"] = sorted(kinds["system_prompt_section"])
    servers = sorted(set(kinds.get("portable_mcp", ())) | {
        s for s, owner in manager._portable_mcp_server_plugins.items() if owner == plugin_key})
    if servers:
        deferred["mcp_servers"] = servers
    summary = {"name": name, "key": plugin_key, "activated_now": now, "deferred": deferred}
    if load_error:
        summary["load_error"] = load_error
    # Read in the process that loaded the plugin (for a gateway answer, the gateway): a dependency
    # installed after it booted is not importable there until it restarts.
    from hermes_cli.plugins_loader import missing_python_dependencies
    if missing := missing_python_dependencies(manifest):
        summary["missing_dependencies"] = missing
    return summary


def activation_summaries(manager: Any) -> list[dict[str, Any]]:
    """One summary per loaded (non-deferred-platform, non-errored) plugin — the ``on_plugin_loaded`` payload."""
    out = []
    for key, loaded in list(manager._plugins.items()):
        if getattr(loaded, "deferred", False) or getattr(loaded, "error", None):
            continue
        out.append(plugin_activation_summary(manager, key))
    return out


def dependency_blocked_summaries(manager: Any) -> list[dict[str, Any]]:
    """Plugins that failed to load in this process while a declared Python dependency is not
    importable here, as ``{name, key, activated_now: {}, deferred: {}, missing_dependencies}``. A
    plugin importing a just-installed dependency at load time fails in a process booted before the
    install; without this entry the caller would read "loaded nothing, needs nothing" instead of
    "restart". Never part of the ``on_plugin_loaded`` payload: nothing of these plugins loaded."""
    from hermes_cli.plugins_loader import missing_python_dependencies
    out = []
    for key, loaded in list(manager._plugins.items()):
        manifest = getattr(loaded, "manifest", None)
        if getattr(loaded, "error", None) and (missing := missing_python_dependencies(manifest)):
            out.append({"name": getattr(manifest, "name", None) or key, "key": key, "activated_now": {},
                        "deferred": {}, "missing_dependencies": missing})
    return out


def find_activation(summaries: Optional[list[dict[str, Any]]], name: str) -> Optional[dict[str, Any]]:
    """The summary for ``name`` (manifest name, canonical key, or bare leaf of the key)."""
    for entry in summaries or ():
        key = str(entry.get("key") or "")
        if name in (entry.get("name"), key, key.rsplit("/", 1)[-1]):
            return entry
    return None


def activate_plugin_now(name: str, *, in_process: bool = True) -> dict[str, Any]:
    """After an install/enable/update: load the plugin in THIS process (so ``on_plugin_loaded``
    subscribers here — the TUI/Desktop server — see it), connect its MCP servers and hand them plus
    its skills to the open chats of this profile (:func:`load_and_go_live`), and nudge the running
    gateway to load it too over its control socket so live adapters re-wire their handlers. A caller
    in another process (``hermes plugins install``) passes ``in_process=False``; the running Desktop /
    dashboard backend is then asked to do the in-process half (:func:`notify_serve_backend`). Never raises.

    Returns ``{"gateway_reloaded": bool, "activation": summary | None, "restart_required": bool}``.
    ``activation.live_now`` lists what is usable in open chats now; ``activation.deferred`` what waits
    for the next session. ``restart_required`` is True when no gateway answered (old gateway, not
    running), or when this process or the gateway cannot import the plugin's Python dependencies
    (``activation.missing_dependencies``; ``activation.missing_in`` names which: ``gateway`` and/or
    ``backend``, the Desktop / dashboard process): a running process never switches to the dependency
    generation an enable just installed, so only restarting that process loads them."""
    from hermes_constants import get_hermes_home
    activation: Optional[dict[str, Any]] = load_and_go_live(name) if in_process else None
    backend: Optional[dict[str, Any]] = None  # out of process: what the Desktop backend did
    if not in_process:
        answer = notify_serve_backend(name, Path(get_hermes_home()))
        if answer is not None:
            activation = answer.get("activation")
            backend = ({"error": answer["backend_error"]} if answer.get("backend_error")
                       else {"activation": activation})
    answer = reload_running_gateway()
    reloaded = bool(answer and answer.get("reloaded"))
    gateway_activation = find_activation((answer or {}).get("activations"), name) if reloaded else None
    gaps = {"gateway": (gateway_activation or {}).get("missing_dependencies") or [],
            "backend": (activation or {}).get("missing_dependencies") or []}
    if reloaded and activation is None:
        activation = gateway_activation
    missing = sorted({*gaps["gateway"], *gaps["backend"]})
    if missing:
        activation = {**(activation or {"name": name, "key": name}), "missing_dependencies": missing,
                      "missing_in": [process for process, deps in gaps.items() if deps]}
    if gateway_error := (gateway_activation or {}).get("load_error"):
        # The gateway's own import result decides what the gateway serves, whatever this process loaded.
        now = {k: v for k, v in ((activation or {}).get("activated_now") or {}).items() if k != "provider"}
        activation = {**(activation or gateway_activation), "activated_now": now, "load_error": gateway_error}
    result = {"gateway_reloaded": reloaded, "activation": activation,
              "restart_required": not reloaded or bool(missing)}
    if backend is not None:
        result["backend"] = backend
    return result


def reload_running_gateway() -> Optional[dict[str, Any]]:
    """Ask the gateway serving this profile (its own socket, else the multiplexer's for a served
    secondary) to force plugin re-discovery, so an enable loads and a disable unloads in the running
    gateway now. The ``reload-plugins`` answer, or None when no gateway answered."""
    try:
        from gateway.control_socket import reload_gateway_plugins
        from hermes_constants import get_default_hermes_root, get_hermes_home
        home = Path(get_hermes_home())
        answer = reload_gateway_plugins(home)
        if answer is None:
            root = Path(get_default_hermes_root())
            if root.resolve() != home.resolve():  # a served secondary: the multiplexer's socket
                answer = reload_gateway_plugins(root, profile_home=home)
        return answer
    except Exception:
        logger.debug("gateway reload-plugins nudge failed", exc_info=True)
        return None


def gateway_reload_hint(answer: Optional[dict[str, Any]], name: str) -> str:
    """One honest sentence after disabling plugin ``name``, a change only the gateway's re-discovery
    applies (an open session stops running its commands at once). Callbacks the plugin already
    wired into platform adapters outlive a reload (the plugin guide), so a restart still ends those."""
    if answer and answer.get("reloaded"):
        return (f"The running gateway reloaded its plugins and unloaded {name}; platform hooks it already "
                "wired up stay until a gateway restart (hermes gateway restart).")
    if answer is not None:  # a gateway answered and refused: say so, with its reason
        return (f"The running gateway refused to reload its plugins: {answer.get('error') or 'no reason given'}.\n"
                "Only the saved config changed; restart the gateway serving this profile to apply it:\n"
                "  hermes gateway restart")
    return ("No running gateway answered. If one is running, restart it for this to take effect:\n"
            "  hermes gateway restart")


def load_and_go_live(name: str) -> Optional[dict[str, Any]]:
    """Force-rediscover plugins in THIS process under the caller's profile scope, connect ``name``'s
    MCP servers and hand them (and its skills) to this profile's open chats with a turn note. Returns
    the activation summary with ``live_now: {mcp_servers, skills}``; ``deferred`` then keeps only what
    waits for the next session (Python ``tools``, ``prompt``). None when the plugin did not load, except
    a load that failed on a dependency this process cannot import: that returns the
    :func:`dependency_blocked_summaries` entry, so the caller asks for a restart."""
    # A forced rediscovery unloads every plugin before it loads them again, which drops their server
    # configs, skills and liveness declarations for the length of the pass. Two installs finishing
    # together (one card, two rows) each go live; the second one's pass must not run while the first
    # reads or connects, or the first plugin comes up with no tools. One go-live at a time.
    with _GO_LIVE_LOCK:
        return _go_live(name)


def _go_live(name: str) -> Optional[dict[str, Any]]:
    from hermes_cli.plugins_activation_live import connect_plugin_mcp, live_notice, plugin_skills
    try:
        from hermes_cli.plugins import _join_background_discovery, get_plugin_manager
        _join_background_discovery()
        manager = get_plugin_manager()
        # Other forced passes (a reload-plugins verb, the dashboard) do not take the go-live lock, so the
        # reads share the discovery lock with the pass that produced them.
        with manager._discovery_lock:
            manager.discover_and_load(force=True)
            activation = find_activation(activation_summaries(manager), name)
            blocked = None if activation else find_activation(dependency_blocked_summaries(manager), name)
            portable = manager.get_portable_mcp_servers()
            skills = plugin_skills(activation["key"]) if activation else []
    except Exception:
        logger.debug("in-process plugin reload after change to %r failed", name, exc_info=True)
        return None
    if activation is None:
        return blocked
    servers = connect_plugin_mcp(activation, portable)
    activation["live_now"] = {"mcp_servers": servers, "skills": skills}
    activation["deferred"] = {k: v for k, v in (activation.get("deferred") or {}).items() if k != "mcp_servers"}
    import sys
    server = sys.modules.get("tui_gateway.server")  # loaded == this process hosts chats
    note = live_notice(activation)
    if server is not None and (servers or note):
        from hermes_constants import get_hermes_home
        try:
            server.refresh_plugin_sessions(Path(get_hermes_home()), activation["key"], note)
        except Exception:
            logger.warning("open chats were not refreshed after activating %r", name, exc_info=True)
    return activation


def serve_backend_records() -> list:
    """Every live, token-consistent chat-hosting backend record on this host: the host owner
    (``serve``) first, then the Desktop child (``desktop-serve``). Both can run at once (a supervised
    dashboard plus Desktop), and Desktop chats live in the child, so an activation must reach each
    (t_01a19a63867979de). A stale (dead pid) record is skipped by ``read_record``; one whose token no
    longer matches is skipped here. Then every per-process entry (``gateway.host_backends``) not
    already listed: a second Desktop-owned backend holds no role record (t_01a1ce929e118cf2).
    Never raises for one bad record."""
    from gateway import host_backends as hb
    from gateway import host_rendezvous as hr
    records, seen = [], set()
    for role in (hr.ROLE_SERVE, hr.ROLE_DESKTOP_SERVE):
        try:
            record = hr.read_record(role)
            usable = record is not None and record.port and hr.record_token_is_consistent(record)
        except Exception:
            logger.debug("serve backend record %s unreadable", role, exc_info=True)
            continue
        if usable and (record.pid, record.port) not in seen:
            seen.add((record.pid, record.port))
            records.append(record)
    try:
        entries = hb.read_entries()
    except Exception:
        logger.debug("serve backend entries unreadable", exc_info=True)
        entries = []
    for record in entries:
        if record.port and (record.pid, record.port) not in seen and hb.token_is_consistent(record):
            seen.add((record.pid, record.port))
            records.append(record)
    return records


def _serve_backend_record():
    """The first of :func:`serve_backend_records` (host owner, else the Desktop child), or None.
    Kept for callers that need one backend; activation uses every record."""
    records = serve_backend_records()
    return records[0] if records else None


def _backend_failure_reason(exc: BaseException) -> str:
    """``<status> <reason>`` for an HTTP refusal, else the transport error; never a header value."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        return f"{exc.code} {exc.reason}".strip()
    if isinstance(exc, urllib.error.URLError):
        return f"unreachable: {exc.reason}"
    return type(exc).__name__


def backend_label(record) -> str:
    """``<role>:<port>`` for log lines and hints."""
    return f"{getattr(record, 'role', 'serve')}:{getattr(record, 'port', '?')}"


def notify_serve_backend_record(record, name: str, home: Path) -> dict[str, Any]:
    """POST ``/api/dashboard/agent-plugins/activate`` for ``name`` in ``home`` to ONE backend record.
    The backend's answer, or ``{"backend_error": "<status> <reason>"}`` (logged at warning) when it
    did not answer. Never raises."""
    try:
        import json
        import urllib.request
        from gateway import host_backends as hb
        from gateway import host_rendezvous as hr
        from hermes_cli.url_utils import format_url_host
        token = hb.token_for(record)
        request = urllib.request.Request(
            f"http://{format_url_host(hr.dial_host(record))}:{record.port}"
            "/api/dashboard/agent-plugins/activate",
            data=json.dumps({"name": name, "home": str(home)}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-Hermes-Session-Token": token})
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read().decode("utf-8"))
        if not isinstance(answer, dict):
            raise ValueError("backend answer is not an object")
        return answer
    except (OSError, ValueError) as exc:  # HTTPError/URLError/timeouts are OSError; bad JSON ValueError
        reason = _backend_failure_reason(exc)
        logger.warning("the %s backend on port %s did not activate plugin %r: %s",
                       getattr(record, "role", "serve"), getattr(record, "port", "?"), name, reason)
        return {"backend_error": reason}
    except Exception as exc:  # health: allow BLE001 -- documented never-raises CLI boundary; traceback logged
        logger.warning("the %s backend activation of plugin %r failed",
                       getattr(record, "role", "serve"), name, exc_info=True)
        return {"backend_error": type(exc).__name__}


def notify_serve_backends(name: str, home: Path, records: Optional[list] = None) -> list[dict[str, Any]]:
    """Ask EVERY running dashboard / Desktop backend (:func:`serve_backend_records`, or ``records``)
    to run :func:`load_and_go_live` for ``name`` in ``home``. One entry per backend:
    ``{"role", "port", "pid", "create_time", "answer"}``; a backend that fails does not stop the
    others. Never raises."""
    if records is None:
        try:
            records = serve_backend_records()
        except Exception:
            logger.debug("serve backend record lookup failed", exc_info=True)
            records = []
    return [{"role": getattr(record, "role", "serve"), "port": getattr(record, "port", None),
             "pid": getattr(record, "pid", None), "create_time": getattr(record, "create_time", None),
             "answer": notify_serve_backend_record(record, name, home)} for record in records]


def notify_serve_backend(name: str, home: Path) -> Optional[dict[str, Any]]:
    """Activate ``name`` in every running backend (:func:`notify_serve_backends`) and fold the answers
    into one: the first backend that answered (host owner first), with ``backends`` listing each
    backend's answer. None when no backend record exists. When any backend did not answer,
    ``backend_error`` says which (``<role>:<port> <reason>`` once there is more than one backend), so
    a caller never reports a skipped Desktop backend as loaded. Never raises."""
    backends = notify_serve_backends(name, home)
    if not backends:
        return None
    answers = [entry["answer"] for entry in backends]
    primary = next((a for a in answers if not a.get("backend_error")), answers[0])
    failed = [e for e in backends if e["answer"].get("backend_error")]
    result = {**primary, "backends": backends}
    if failed:
        result["backend_error"] = "; ".join(
            (f"{e['role']}:{e['port']} " if len(backends) > 1 else "") + str(e["answer"]["backend_error"])
            for e in failed)
    return result


def _gateway_present() -> bool:
    """A gateway runs for this profile (its own, or the multiplexer serving it from the root), or a
    gateway service is installed for it."""
    from gateway.status import get_running_pid
    from hermes_cli.gateway import _installed_service_kind
    from hermes_constants import get_default_hermes_root, get_hermes_home
    homes = {Path(get_hermes_home()).resolve(), Path(get_default_hermes_root()).resolve()}
    return (any(get_running_pid(home / "gateway.pid", cleanup_stale=False) for home in homes)
            or _installed_service_kind() is not None)


_RESTART_LINES = {"gateway": "  hermes gateway restart",
                  "backend": "  quit and reopen the Hermes desktop app (or restart hermes dashboard)"}


def dependency_restart_hint(missing: list[str], processes: list[str]) -> str:
    """What to do when running processes cannot import a plugin's dependencies: one restart line per
    process that reported the gap (``gateway`` / ``backend``), never one that can already import them."""
    from hermes_cli.plugins_loader import installed_in_selected_environment
    deps = ", ".join(missing)
    restart = "\n".join(_RESTART_LINES[p] for p in processes)
    if installed_in_selected_environment(missing):
        return (f"Its Python dependencies ({deps}) were installed after the running gateway or dashboard "
                "started, and a running process never switches dependency environments. Restart them to "
                f"load the plugin:\n{restart}")
    return f"Its Python dependencies ({deps}) are not installed. Run hermes pm repair, then restart:\n{restart}"


def report_activation(console, name: str) -> None:
    """After a CLI install/enable: load *name* where it can go live now (:func:`activate_plugin_now`)
    and print :func:`activation_hint`, or nothing when there is nothing to say."""
    if hint := activation_hint(activate_plugin_now(name, in_process=False)):
        console.print(f"[dim]{hint}[/dim]")


def activation_hint(result: dict[str, Any]) -> str:
    """One honest sentence for CLI surfaces from an :func:`activate_plugin_now` result; empty when
    there is nothing to say. When a Desktop / dashboard backend was asked (``result["backend"]``), a
    first line says whether it loaded the plugin."""
    body = _activation_body(result)
    line = _backend_line(result.get("backend"))
    return "\n".join(x for x in (line, body) if x)


def _backend_line(backend: Optional[dict[str, Any]]) -> str:
    if not backend:
        return ""
    if error := backend.get("error"):
        return (f"The Desktop backend did not load it ({error}); open Desktop chats get it after that "
                "backend restarts.")
    act = backend.get("activation")
    if act and act.get("missing_dependencies"):
        return ""  # the dependency restart hint below names the backend
    if not act:
        return ("The Desktop backend did not load it (it answered without loading the plugin); open "
                "Desktop chats get it after that backend restarts.")
    now = act.get("activated_now") or {}
    if now:
        return "Desktop backend loaded it — active in open Desktop chats now: " + ", ".join(sorted(now)) + "."
    return "Desktop backend loaded it."


def _activation_body(result: dict[str, Any]) -> str:
    act = result.get("activation") or {}
    live = act.get("live_now") or {}
    connected = [s for s in live.get("mcp_servers") or () if s.get("connected")]
    lines = []
    if connected or live.get("skills"):
        tools = sum(len(s.get("tools") or ()) for s in connected)
        lines.append(f"Live in open chats now: {tools} MCP tools, {len(live.get('skills') or ())} skills.")
    lines.extend(f"MCP server {s['name']} not connected: {s.get('error') or 'unknown error'}"
                 for s in live.get("mcp_servers") or () if not s.get("connected"))
    if missing := act.get("missing_dependencies"):
        return "\n".join([*lines, dependency_restart_hint(missing, act.get("missing_in") or [])])
    if error := act.get("load_error"):
        return "\n".join([*lines, f"The updated provider plugin failed to load ({error}); "
                                  "the previous version keeps serving."])
    if not result.get("gateway_reloaded"):
        # Live in open chats, or no gateway runs or is installed for this profile (first-time setup):
        # a messaging gateway that starts later loads it at boot, so a restart applies to nothing.
        if lines or not _gateway_present():
            return "\n".join(lines)
        return "\n".join([*lines, "Restart the gateway for the plugin to take effect:\n  hermes gateway restart"])
    now, deferred = act.get("activated_now") or {}, act.get("deferred") or {}
    parts = []
    if now:
        now_labels = {"provider": "model provider (new sessions use this version)"}
        parts.append("active in the running gateway now: " + ", ".join(now_labels.get(k, k) for k in sorted(now)))
    if deferred:
        labels: dict[str, str] = {"tools": "tools (next session)", "prompt": "system prompt (next session)",
                                  "mcp_servers": "MCP servers (next session)"}
        parts.append("deferred: " + ", ".join(labels.get(k, k) for k in sorted(deferred)))
    if not parts:
        return "\n".join([*lines, "Gateway reloaded plugins; nothing of this plugin needs a session or restart."])
    return "\n".join([*lines, "Gateway reloaded plugins — " + "; ".join(parts) + "."])
