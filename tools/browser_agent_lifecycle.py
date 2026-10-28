"""Opt-in lifecycle client for a dedicated Chrome agent (never personal Chrome).

The supervisor owns persistence, target attribution and idle decisions. This module
only keeps this process's capabilities for task-scoped release. A dead worker's
request protection expires at the supervisor; open daemon sockets are not activity.
"""
import contextlib
import contextvars
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_lock = threading.RLock()

@dataclass
class _Lease:
    config: dict
    capability: str = ""
    env: dict = field(default_factory=dict)
    active: int = 0
    closing: bool = False
    supervisor: str = ""
    retired: list = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)


_leases: dict[tuple[str, str, str, str], _Lease] = {}
_vault_owners: dict[tuple[str, str], str] = {}
# (home, task, supervisor key, lease endpoint) pinned by renew_selected for one vault operation.
_selected_scope: contextvars.ContextVar = contextvars.ContextVar("hermes_managed_browser_selected", default=None)


def supervisor_task(task_id: str) -> str:
    """Vault's existing no-session API addresses the last selected scope, never a union. Inside
    :func:`renew_selected` the scope captured (and leased) at the start of the operation wins, so a
    concurrent call that selects another scope cannot redirect a fill mid-operation."""
    held = _selected_scope.get()
    if held and held[:2] == (str(get_hermes_home()), task_id):
        return held[2]
    with _lock:
        return _vault_owners.get((str(get_hermes_home()), task_id), task_id)


def supervisor_identity(task_id: str, capability: str) -> str:
    return 'managed-' + hashlib.sha256(json.dumps(
        [str(get_hermes_home()), task_id, capability]).encode()).hexdigest()


class ManagedBrowserError(RuntimeError):
    """An explicitly managed route cannot safely proceed."""


def configured_route(browser_cfg: dict, env: dict) -> dict | None:
    """Match the exact operator override before any CDP discovery or profile launch."""
    config = browser_cfg.get("managed_agent")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ManagedBrowserError("browser.managed_agent must be a mapping")
    if config == {"endpoint": "", "installation_id": ""}:
        return None
    endpoint = config.get("endpoint")
    if not isinstance(endpoint, str):
        raise ManagedBrowserError("browser.managed_agent.endpoint is required")
    from tools.browser_tool_cdp import _get_cdp_override_raw
    selected = env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or _get_cdp_override_raw()
    if selected != endpoint:
        return None  # cloud, personal-copy and other explicit routes retain their behavior
    parts = urllib.parse.urlsplit(endpoint)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
            or parts.path or parts.query or parts.fragment or parts.username or parts.password):
        raise ManagedBrowserError("Managed agent endpoint must be an exact loopback HTTP root")
    identity = config.get("installation_id")
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ManagedBrowserError("browser.managed_agent.installation_id is required")
    if set(config) - {"endpoint", "installation_id"}:
        raise ManagedBrowserError("Unknown managed_agent setting; activate the installed listener explicitly")
    return {"endpoint": endpoint, "installation_id": identity}


def _post(config: dict, action: str, **payload) -> dict:
    body = json.dumps({"installation_id": config["installation_id"], **payload}).encode()
    request = urllib.request.Request(config["endpoint"] + "/lifecycle/" + action,
                                     data=body, headers={"Content-Type": "application/json"}, method="POST")
    # A local lifecycle capability must never pass through a configured HTTP proxy or redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=120 if action == "acquire" else 15) as response:
            result = json.loads(response.read(65537))
        if not isinstance(result, dict) or result.get("error"):
            raise ManagedBrowserError("Managed browser rejected lifecycle " + action)
        return result
    except urllib.error.HTTPError as exc:
        # Do not echo response text: capabilities and URLs are not model-visible errors.
        if exc.code == 403:
            raise ManagedBrowserError("Managed browser deliberately stopped/disabled, identity invalid, or lease refused; operator activation may be required") from None
        raise ManagedBrowserError(f"Managed browser lifecycle unavailable (HTTP {exc.code})") from None
    except (OSError, ValueError) as exc:
        raise ManagedBrowserError("Managed browser listener unavailable; idle wake requires the activated listener, not a stopped installation") from exc


def _validate_grant(config: dict, result: dict) -> None:
    for field in ("capability", "request_id", "daemon_name"):
        value = result.get(field)
        if not isinstance(value, str) or not _TOKEN.fullmatch(value):
            raise ManagedBrowserError("Managed browser returned an invalid lease grant")
    if (result.get("endpoint") != config["endpoint"] + "/lease/" + result["capability"]
            or result["daemon_name"] != "lease-" + result["capability"]):
        raise ManagedBrowserError("Managed browser lease endpoint or daemon identity mismatch")


@contextlib.contextmanager
def request_lease(config: dict, task_id: str | None, session: str, timeout: int, env: dict):
    if not task_id:
        raise ManagedBrowserError("Managed browser requires a tool task/session identity; refusing shared default ownership")
    with _owner_lease(config, task_id, f"named:{session}" if session else "default", timeout, env) as grant:
        yield grant


# Owner scope of the built-in browser tools (agent-browser, browser_cdp, browser_dialog). It can
# never collide with a browser_exec scope ("default" / "named:<session>").
TOOLS_SCOPE = "tools"
_tool_scope: contextvars.ContextVar = contextvars.ContextVar("hermes_managed_browser_tool_scope", default=None)


def _managed_config(tool: str) -> dict | None:
    """The managed route for built-in tools: ``browser.managed_agent`` matching the raw override."""
    from tools.browser_use_cli import _read_browser_cfg
    try:
        return configured_route(_read_browser_cfg(), {})
    except ManagedBrowserError as exc:
        raise ManagedBrowserError(f"{tool}: {exc}") from None


@contextlib.contextmanager
def tool_lease(tool: str, task_id: str | None, timeout: float):
    """Hold an owner lease on the managed Chrome while a built-in browser tool runs.

    Yields None (and does nothing else) when ``browser.managed_agent`` does not match the selected
    CDP override. Otherwise yields the lease-scoped HTTP root; :func:`scoped_override` returns it to
    every CDP resolution inside the block, so nothing reaches the unscoped endpoint. Nested calls
    for the same task reuse the outer lease. Released by :func:`release_task` / :func:`release_all`.
    """
    config = _managed_config(tool)
    if config is None:
        yield None
        return
    if not task_id:
        raise ManagedBrowserError(f"{tool}: managed browser requires a task identity; refusing shared default ownership")
    current = _tool_scope.get()
    if current and current[:3] == (str(get_hermes_home()), task_id, config["endpoint"]):
        yield current[3]
        return
    timeout = max(1, min(86400, int(timeout or 0) or 1))
    try:
        with _owner_lease(config, task_id, TOOLS_SCOPE, timeout, None) as grant:
            token = _tool_scope.set((str(get_hermes_home()), task_id, config["endpoint"], grant["endpoint"]))
            try:
                yield grant["endpoint"]
            finally:
                _tool_scope.reset(token)
    except ManagedBrowserError as exc:
        message = str(exc)
        raise ManagedBrowserError(message if message.startswith(tool + ":") else f"{tool}: {message}") from None


def scoped_override(raw: str, tool: str = "browser") -> str:
    """Lease-scoped replacement for the raw CDP override, "" when unmanaged.

    Raises ManagedBrowserError when the override is the managed endpoint but no tool lease is held:
    a caller outside :func:`tool_lease` must fail, never fall back to the unscoped root.
    """
    current = _tool_scope.get()
    if current and current[2] == raw and current[0] == str(get_hermes_home()):
        return current[3]
    if _managed_config(tool) is None:
        return ""
    raise ManagedBrowserError(f"{tool}: managed Chrome requires an owner lease; this path ran outside one")


def current_tool_lease() -> str:
    """Lease endpoint held by the enclosing :func:`tool_lease`, or ""."""
    current = _tool_scope.get()
    return current[3] if current and current[0] == str(get_hermes_home()) else ""


@contextlib.contextmanager
def renew_selected(tool: str, task_id: str | None, timeout: float):
    """Keep the scope the vault addresses (the last browser_exec or built-in tool scope) leased.

    Unmanaged: no-op. No scope selected yet: a fresh built-in tool lease. A rotated capability
    means the selected scope's tabs and supervisor are gone, so fail instead of acting elsewhere.
    """
    config = _managed_config(tool)
    if config is None:
        yield None
        return
    if not task_id:
        raise ManagedBrowserError(f"{tool}: managed browser requires a task identity; refusing shared default ownership")
    home = str(get_hermes_home())
    with _lock:
        selected = _vault_owners.get((home, task_id))
        found = next(((key, record) for key, record in _leases.items()
                      if key[:2] == (home, task_id) and key[3] == config["endpoint"]
                      and record.supervisor == selected and record.capability and not record.closing), None)
    if found is None or found[0][2] == TOOLS_SCOPE:
        previous = found[1].capability if found else ""
        with tool_lease(tool, task_id, timeout) as endpoint:
            if previous and not endpoint.endswith("/lease/" + previous):
                raise ManagedBrowserError(f"{tool}: the managed browser lease for this task expired, so its tabs "
                                          "are gone; open the page again (browser_navigate) and retry")
            token = _selected_scope.set((home, task_id, task_id, endpoint))
            try:
                yield endpoint
            finally:
                _selected_scope.reset(token)
        return
    key, record = found
    previous = record.capability
    timeout = max(1, min(86400, int(timeout or 0) or 1))
    try:
        with _owner_lease(config, task_id, key[2], timeout, dict(record.env) if record.env else None) as grant:
            if grant["capability"] != previous:
                raise ManagedBrowserError(f"{tool}: the managed browser lease for the selected browser_exec "
                                          "session expired, so its tabs are gone; run browser_exec in that "
                                          "session again and retry")
            token = _selected_scope.set((home, task_id, selected, grant["endpoint"]))
            try:
                yield grant["endpoint"]
            finally:
                _selected_scope.reset(token)
    except ManagedBrowserError as exc:
        message = str(exc)
        raise ManagedBrowserError(message if message.startswith(tool + ":") else f"{tool}: {message}") from None


def supervisor_in_scope(supervisor) -> bool:
    """Whether a cached CDP supervisor belongs to the managed lease this call holds.

    Inside a tool lease (or a vault operation's pinned scope) a supervisor attached through any other
    endpoint (an older capability, or a pre-activation raw attach) must not be used. Outside any
    managed lease the answer is always True, so unmanaged behavior is unchanged."""
    held = _selected_scope.get()
    lease = held[3] if held and held[0] == str(get_hermes_home()) else current_tool_lease()
    if not lease:
        return True
    parts = urllib.parse.urlsplit(lease)
    prefix = f"ws://{parts.netloc}{parts.path}/"
    return str(getattr(supervisor, "cdp_url", "") or "").startswith(prefix)


@contextlib.contextmanager
def _owner_lease(config: dict, task_id: str, owner_session: str, timeout: int, env: dict | None):
    """Acquire/complete one request on the owner's lease; ``env`` (browser_exec only) is pointed at the
    lease and remembered so the harness daemon can be shut down on release. Without ``env`` the
    scope is a built-in tool scope whose supervisor is keyed by the task id."""
    owner = (str(get_hermes_home()), task_id, owner_session)
    key = (*owner, config["endpoint"])
    with _lock:
        pending = _leases.get(key)
    if pending and pending.closing:
        _release_record(key, pending)
    with _lock:
        record = _leases.setdefault(key, _Lease(dict(config)))
        if record.closing:
            raise ManagedBrowserError("Managed browser owner cleanup is pending")
        record.active += 1
    grant = None
    try:
        # Serialize this owner's grant mutation, never unrelated owners or cleanup.
        with record.lock:
            extra = {"daemon_dir": _daemon_dir(env)} if env is not None else {}
            result = _post(config, "acquire", owner=list(owner), timeout_s=timeout,
                           worker_pid=os.getpid(), **extra)
            _validate_grant(config, result)
            grant = result
            if env is not None:
                env.pop("BU_CDP_WS", None)
                env.update(BU_CDP_URL=grant["endpoint"], BU_NAME=grant["daemon_name"],
                           BH_RUNTIME_DIR_SHARED="1", BH_TMP_DIR_SHARED="1", BU_AUTOSPAWN="0")
            supervisor = supervisor_identity(task_id, grant["capability"]) if env is not None else task_id
            with _lock:
                if record.capability and record.capability != grant["capability"]:
                    # A built-in tool scope keeps its supervisor key across rotations: never let
                    # disposing the old capability stop the supervisor the new one re-attaches.
                    record.retired.append(_Lease(record.config, capability=record.capability, env=record.env,
                                                 supervisor="" if record.supervisor == supervisor else record.supervisor))
                record.capability = grant["capability"]
                record.env = dict(env) if env is not None else {}
                record.supervisor = supervisor
                _vault_owners[owner[:2]] = record.supervisor
        yield grant
    finally:
        if grant:
            try:
                _post(config, "complete", capability=grant["capability"], request_id=grant["request_id"])
            except ManagedBrowserError:
                logger.warning("Managed browser completion unavailable; request deadline remains bounded")
        with _lock:
            record.active -= 1
            release = record.closing and not record.active
            if not record.capability and not record.active:
                _leases.pop(key, None)
        if release:
            _release_record(key, record)
        else:
            _release_retired(record)


def _daemon_dir(env: dict) -> str:
    """Match harness paths without importing its environment-bound IPC module."""
    from pathlib import Path
    runtime = env.get("BH_RUNTIME_DIR") or env.get("BH_TMP_DIR")
    if runtime:
        return str(Path(runtime).expanduser().resolve())
    home = env.get("BH_HOME") or env.get("BROWSER_HARNESS_HOME")
    if not home:
        home = str(Path(env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "browser-harness")
    return str(Path(home).expanduser().resolve() / "runtime")


def _release_record(key, record) -> None:
    with record.lock:
        with _lock:
            if _leases.get(key) is not record or record.active:
                return
        _release_retired(record)
        if _dispose(record) and not record.retired:
            with _lock:
                if _leases.get(key) is record:
                    del _leases[key]
                    if _vault_owners.get(key[:2]) == record.supervisor:
                        del _vault_owners[key[:2]]


def _dispose(record) -> bool:
    from tools import browser_use_cli as cli
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    try:
        _post(record.config, "release", capability=record.capability)
        cmd = cli._find_cli()
        if cmd and record.env:
            result = cli._run_cli_killing_process_group([*cmd, "--reload"], "", record.env, 15)
            if result.returncode:
                raise ManagedBrowserError("Owned harness daemon shutdown failed")
        if record.supervisor:
            SUPERVISOR_REGISTRY.stop(record.supervisor)
    except (ManagedBrowserError, OSError, subprocess.SubprocessError):
        logger.warning("Managed browser cleanup unavailable; retaining capability for retry")
        return False
    return True


def _release_retired(record) -> None:
    with record.lock:
        with _lock:
            if record.active:
                return
            retired, record.retired = record.retired, []
        failed = [old for old in retired if not _dispose(old)]
        with _lock:
            record.retired.extend(failed)


def _release_matching(predicate) -> None:
    with _lock:
        records = [(key, record) for key, record in _leases.items() if predicate(key)]
        for _, record in records:
            record.closing = True
    for key, record in records:
        if not record.active:
            _release_record(key, record)


def release_task(task_id: str) -> None:
    """Never release another profile's identically named task or a sibling task."""
    home = str(get_hermes_home())
    _release_matching(lambda key: key[:2] == (home, task_id))


def release_all() -> None:
    """Process shutdown only: records carry the original installation and profile."""
    _release_matching(lambda key: True)
