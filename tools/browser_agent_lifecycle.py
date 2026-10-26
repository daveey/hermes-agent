"""Opt-in lifecycle client for a dedicated Chrome agent (never personal Chrome).

The supervisor owns persistence, target attribution and idle decisions. This module
only keeps this process's capabilities for task-scoped release. A dead worker's
request protection expires at the supervisor; open daemon sockets are not activity.
"""
import contextlib
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


def supervisor_task(task_id: str) -> str:
    """Vault's existing no-session API addresses the last selected scope, never a union."""
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
    owner = (str(get_hermes_home()), task_id, f"named:{session}" if session else "default")
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
            result = _post(config, "acquire", owner=list(owner), timeout_s=timeout,
                           worker_pid=os.getpid(), daemon_dir=_daemon_dir(env))
            _validate_grant(config, result)
            grant = result
            env.pop("BU_CDP_WS", None)
            env.update(BU_CDP_URL=grant["endpoint"], BU_NAME=grant["daemon_name"],
                       BH_RUNTIME_DIR_SHARED="1", BH_TMP_DIR_SHARED="1", BU_AUTOSPAWN="0")
            with _lock:
                if record.capability and record.capability != grant["capability"]:
                    record.retired.append(_Lease(record.config, capability=record.capability,
                                                 env=record.env, supervisor=record.supervisor))
                record.capability = grant["capability"]
                record.env = dict(env)
                record.supervisor = supervisor_identity(task_id, record.capability)
                _vault_owners[owner[:2]] = record.supervisor
        yield
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
