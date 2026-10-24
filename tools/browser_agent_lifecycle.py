"""Opt-in lifecycle client for a dedicated Chrome agent (never personal Chrome).

The supervisor owns persistence, target attribution and idle decisions. This module
only keeps this process's capabilities for task-scoped release. A dead worker's
request protection expires at the supervisor; open daemon sockets are not activity.
"""
import contextlib
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_lock = threading.RLock()
_leases: dict[tuple[str, str, str, str], tuple[dict, str]] = {}


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
        with opener.open(request, timeout=60) as response:
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
        grant = _post(config, "acquire", owner=list(owner), timeout_s=timeout)
        _validate_grant(config, grant)
        _leases[key] = (dict(config), grant["capability"])
    env.pop("BU_CDP_WS", None)
    env["BU_CDP_URL"] = grant["endpoint"]
    env["BU_NAME"] = grant["daemon_name"]
    # Caller-provided harness directories otherwise use a bare 'bu' socket,
    # silently ignoring BU_NAME and merging independently leased owners.
    env["BH_RUNTIME_DIR_SHARED"] = "1"
    env["BH_TMP_DIR_SHARED"] = "1"
    env["BU_AUTOSPAWN"] = "0"  # failed owned setup must not spawn an unrelated cloud browser
    try:
        yield
    finally:
        try:
            _post(config, "complete", capability=grant["capability"], request_id=grant["request_id"])
        except ManagedBrowserError:
            logger.warning("Managed browser completion unavailable; request deadline remains bounded")


def _release(keys) -> None:
    for key in keys:
        config, capability = _leases[key]
        try:
            _post(config, "release", capability=capability)
        except ManagedBrowserError:
            logger.warning("Managed browser release unavailable; retaining capability for retry")
        else:
            _leases.pop(key, None)


def release_task(task_id: str) -> None:
    """Never release another profile's identically named task or a sibling task."""
    home = str(get_hermes_home())
    with _lock:
        _release([key for key in _leases if key[:2] == (home, task_id)])


def release_all() -> None:
    """Process shutdown only: records carry the original installation and profile."""
    with _lock:
        _release(list(_leases))
