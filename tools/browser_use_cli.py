"""``browser_exec``: drive a real browser through the Browser Use CLI engine (browser-harness,
https://browser-use.com), Hermes' only core browser tool.

The harness speaks CDP to whatever ``tools.browser_session`` resolved for the call: an operator
``BU_CDP_*`` endpoint, a ``/browser connect`` override, a cloud provider's session, or a local browser
(real profile, Lightpanda, or the packaged Chromium started by ``tools.browser_launcher``).
"""

import contextlib
import contextvars
import functools
import importlib
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from hermes_cli.process_identity import (
    own_process_groups, register_child, release_process_group_once_empty, spawn_env, spawned_by_this_process)
from hermes_constants import get_hermes_home, hermes_home_key
from tools.browser_harness_dirs import (daemon_env, daemon_identity, fit_default_socket_dir, harness_pid_file,
                                        namespace_harness_dirs)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_BROWSER_USE_PROVIDER = "browser-use"
# The one credential the harness itself reads (BU_AUTOSPAWN of a Browser Use cloud browser).
_HARNESS_PASSTHROUGH_KEYS: tuple[str, ...] = ("BROWSER_USE_API_KEY",)

# Cloud daemon names become the BU_NAME env var
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Set on the env dict by the CDP resolvers when the resolved browser is EXCLUSIVE to this named session
# (per-name provider / named BU cloud / Lightpanda). Popped before the subprocess launches — never exported.
_PRIVATE_BROWSER_SENTINEL = "_HERMES_BU_PRIVATE_BROWSER"
# Internal route provenance: this exec resolved to a browser on the Bot Desktop display and must use
# the same human-control lease fence as the built-in browser tools. Popped before launching the CLI.
_BOT_DESKTOP_BROWSER_SENTINEL = "_HERMES_BU_BOT_DESKTOP_BROWSER"
# The note when the consented real profile could not load (the route fell back). Popped before launch.
_REAL_PROFILE_FALLBACK_SENTINEL = "_HERMES_BU_REAL_PROFILE_FALLBACK"
# local=True whose real profile could not load: run on the local sidecar browser. Popped before launch.
_LOCAL_FALLBACK_SENTINEL = "_HERMES_BU_LOCAL_FALLBACK"
# Where new/hidden tabs may be brought to the front: a browser_use_shot.ACTIVATE_* mode. Popped before launch.
_CLOUD_BROWSER_SENTINEL = "_HERMES_BU_CLOUD_BROWSER"

# Prepended to the model's code for named sessions on SHARED browsers (a /browser connect CDP override): the
# harness daemon attaches to the first existing page at startup, so two fresh named daemons can land on the
# SAME tab. Steering each onto a tab it created prevents clobbering. Runs once per daemon (marker keyed by
# BU_NAME + daemon pid).
_OWN_TAB_PREAMBLE = """\
# hermes: pin this named session to its own tab (once per daemon process)
def _hermes_ensure_own_tab():
    import os as _os, tempfile as _tf
    _name = _os.environ.get("BU_NAME", "default")
    try:
        # Key the marker by the daemon's pid so a daemon restart (which
        # re-attaches to the first shared page) re-pins automatically,
        # while agent-driven tab switches mid-session are left alone.
        from browser_harness import _ipc as _bipc
        _dpid = _bipc.pid_path(_name).read_text().strip() or "0"
    except Exception:
        _dpid = "0"
    _uid = _os.getuid() if hasattr(_os, "getuid") else 0
    _marker = _os.path.join(
        _tf.gettempdir(), "hermes-bu-owntab-%s-%s-%s" % (_uid, _name, _dpid)
    )
    if _os.path.exists(_marker):
        return
    try:
        # Force a fresh target: new_tab() would REUSE a blank current tab,
        # which is exactly the tab a sibling daemon may also hold.
        # background=True: a foreground createTarget makes macOS hand Chrome
        # keyboard focus, stealing it from the user's app on every new session.
        _tid = cdp("Target.createTarget", url="about:blank", background=True).get("targetId")
        if _tid:
            switch_tab(_tid)
    except Exception:
        pass  # best-effort: worst case is pre-fix behavior
    try:
        open(_marker, "w").close()
    except OSError:
        pass
_hermes_ensure_own_tab()
del _hermes_ensure_own_tab
"""

# [daveey] Agents open tabs in the user's SHARED Chrome (new_tab, the own-tab preamble's createTarget) and
# never close them; with nothing recording which tabs are an agent's, nobody could close them safely
# (2026-10-28: 59 tabs, load 150+). Prepended to every exec on a shared browser, before _OWN_TAB_PREAMBLE so
# its createTarget is recorded too: wraps cdp / new_tab / close_tab in the exec namespace so every target
# this call CREATES lands in the owner's ledger file (HERMES_AGENT_TAB_LEDGER) and every closeTarget drops
# it, and enforces the per-owner and global caps before a create. A tab new_tab() REUSES (blank current
# tab) was not created and is not recorded. Hermes closes only ids in the ledger (close_agent_tabs).
_TAB_LEDGER_PREAMBLE = """\
# hermes: record the tabs this agent creates (tab ledger: caps + close on task end)
def _hermes_tab_ledger():
    import json as _j, os as _o, time as _t
    _g = globals()
    _path = _o.environ.get("HERMES_AGENT_TAB_LEDGER")
    if not _path or "cdp" not in _g or "new_tab" not in _g:
        return
    _dir, _me = _o.path.dirname(_path), _o.path.basename(_path)
    _cap_owner = int(_o.environ.get("HERMES_AGENT_TAB_CAP_OWNER") or 3)
    _cap = int(_o.environ.get("HERMES_AGENT_TAB_CAP") or 8)
    _bu = _o.environ.get("BU_NAME") or "default"
    _raw_cdp, _raw_new, _raw_close = _g["cdp"], _g["new_tab"], _g.get("close_tab")

    def _load(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                d = _j.load(f)
            if isinstance(d, dict) and isinstance(d.get("tabs"), dict):
                return d
        except (OSError, ValueError):
            pass  # missing or corrupt: an empty ledger
        return {"owner": _o.path.basename(p)[:-5], "last_used": 0, "tabs": {}}

    def _save(p, d):
        try:
            _o.makedirs(_dir, exist_ok=True)
            tmp = "%s.%d.tmp" % (p, _o.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                _j.dump(d, f)
            _o.replace(tmp, p)
        except OSError:
            pass  # bookkeeping must never fail the agent's own code

    def _others():
        try:
            names = sorted(n for n in _o.listdir(_dir) if n.endswith(".json") and n != _me)
        except OSError:
            return []
        return [_o.path.join(_dir, n) for n in names]

    def _live():
        try:
            return {t["targetId"] for t in _raw_cdp("Target.getTargets")["targetInfos"]}
        except Exception:
            return None

    def _close(tid):
        try:
            _raw_cdp("Target.closeTarget", targetId=tid)
        except Exception:
            pass  # already gone

    def _oldest(d):
        return sorted(d["tabs"], key=lambda k: d["tabs"][k].get("created", 0))

    def _make_room():
        now, live = _t.time(), _live()
        own = _load(_path)
        own["tabs"] = {k: v for k, v in own["tabs"].items() if live is None or k in live}
        if _cap_owner > 0:
            for tid in _oldest(own)[:max(0, len(own["tabs"]) - _cap_owner + 1)]:
                _close(tid)
                del own["tabs"][tid]
        own["last_used"] = now
        _save(_path, own)
        if _cap <= 0:
            return
        others = sorted(((p, _load(p)) for p in _others()), key=lambda pd: pd[1].get("last_used", 0))
        total = len(own["tabs"]) + sum(1 for _, d in others for k in d["tabs"] if live is None or k in live)
        for p, d in others:
            if total < _cap or now - d.get("last_used", 0) < 600:
                break
            for tid in _oldest(d):
                if total < _cap:
                    break
                if live is None or tid in live:
                    _close(tid)
                    total -= 1
                del d["tabs"][tid]
            _save(p, d)
        if total >= _cap:
            raise RuntimeError(
                "Hermes agent tab limit reached: %d agent-opened tabs are open across tasks (limit %d). "
                "Close tabs you no longer need with close_tab(target_id), then retry." % (total, _cap))

    def _record(tid, url):
        if not tid:
            return
        d = _load(_path)
        d["owner"], d["last_used"] = _me[:-5], _t.time()
        d["tabs"][tid] = {"created": _t.time(), "bu_name": _bu, "url": url or ""}
        _save(_path, d)

    def _forget(tid):
        for p in [_path] + _others():
            d = _load(p)
            if tid in d["tabs"]:
                del d["tabs"][tid]
                _save(p, d)

    def cdp(method, *a, **kw):
        if method == "Target.createTarget":
            _make_room()
            res = _raw_cdp(method, *a, **kw)
            _record((res or {}).get("targetId"), kw.get("url"))
            return res
        res = _raw_cdp(method, *a, **kw)
        if method == "Target.closeTarget":
            _forget(kw.get("targetId"))
        return res

    def new_tab(url="about:blank", *a, **kw):
        _make_room()
        before = _live() or set()
        tid = _raw_new(url, *a, **kw)
        if tid and tid not in before:
            _record(tid, url)
        return tid

    def close_tab(target=None, *a, **kw):
        tid = (target.get("targetId") or target.get("target_id")) if isinstance(target, dict) else target
        if tid is None and "current_tab" in _g:
            tid = (_g["current_tab"]() or {}).get("targetId")
        res = _raw_close(target, *a, **kw)
        _forget(tid)
        return res

    _g["cdp"], _g["new_tab"] = cdp, new_tab
    if _raw_close is not None:
        _g["close_tab"] = close_tab
_hermes_tab_ledger()
del _hermes_tab_ledger
"""

_DEFAULT_TIMEOUT_S = 300
_MIN_TIMEOUT_S = 5
_MAX_TIMEOUT_S = 1800
_STDERR_CAP_CHARS = 4000

_TASK_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")  # filesystem-safe task ids
# Screenshot paths printed by capture_screenshot(): POSIX or Windows drive-letter absolute.
_IMAGE_PATH_RE = re.compile(r"((?:[A-Za-z]:[\\/]|/)[^\s\"']+?\.(?:png|jpe?g|webp))", re.IGNORECASE)
# http(s) URL literals in exec code, checked against the browser URL policy
_URL_RE = re.compile(r"https?://[^\s'\"\\)]+", re.IGNORECASE)
_FHS_BIN_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                 "/usr/bin", "/sbin", "/bin")


def _quiet(fn: Callable[[], Any], default: Any, log_prefix: str = "") -> Any:
    """``fn()``, or ``default`` on any exception (debug-logged when ``log_prefix`` is set)."""
    try:
        return fn()
    except Exception as e:
        if log_prefix:
            logger.debug("%s: %s", log_prefix, e)
    return default


def _lazy_call(module: str, name: str, default: Any, log_prefix: str) -> Any:
    """Call ``module.name()`` resolved at call time (tests stub the module / patch the
    attribute); on any failure log ``log_prefix`` and return ``default``."""
    return _quiet(lambda: getattr(importlib.import_module(module), name)(), default, log_prefix)


def _real_profile_consented() -> bool:
    return _lazy_call("tools.browser_session_cloud", "use_real_profile", False, "real-profile consent lookup failed")


def _set_cdp_env(env: dict, cdp: str) -> None:
    """Export a CDP endpoint under the BU_CDP_* contract (http(s) → URL, else WS)."""
    env["BU_CDP_URL" if cdp.startswith(("http://", "https://")) else "BU_CDP_WS"] = cdp


def _has_cdp_env(env: dict) -> bool:
    return bool(env.get("BU_CDP_WS") or env.get("BU_CDP_URL"))


def _export_session_cdp(env: dict, get_session_info: Callable[[str], Any], cache_key: str,
                        fail_msg: Callable[[Exception], str], no_cdp_msg: str) -> Optional[str]:
    """Export the CDP endpoint from ``get_session_info(cache_key)``; error string on failure / no CDP."""
    try:
        cdp = str((get_session_info(cache_key) or {}).get("cdp_url") or "")
    except Exception as e:
        return fail_msg(e)
    if not cdp:
        return no_cdp_msg
    _set_cdp_env(env, cdp)
    return None


class _CodeUrlVerdict(NamedTuple):
    """What one pass over a code blob's URL literals decided (see ``_code_url_verdict``)."""

    blocked: Optional[str]
    local_sidecar: bool


def _code_url_verdict(code: str) -> _CodeUrlVerdict:
    """One pass over the code's URL literals: the blocking error, if any, and whether the call must run on
    the hybrid LOCAL sidecar. Each URL's routing predicate is evaluated ONCE and feeds both answers: it
    resolves DNS unmemoised, so a rebinding host evaluated twice could relax the private-address floor and
    still route to the cloud provider."""
    from tools.browser_url_policy import evaluate_url_safety, hybrid_routes_locally
    local_sidecar = False
    for url in _URL_RE.findall(code or ""):
        routes_local = hybrid_routes_locally(url)
        local_sidecar = local_sidecar or routes_local
        err = evaluate_url_safety(url, auto_local=routes_local)
        if err:
            return _CodeUrlVerdict(err.get("error", "Blocked: unsafe URL"), local_sidecar)
    return _CodeUrlVerdict(None, local_sidecar)


def _harness_credentials() -> dict:
    """The harness's own credential from the active profile's secret scope, never ``os.environ``: under
    multiplex that holds the LAUNCH profile's key, and a served profile's browser runs on its own (or none)."""
    from agent.secret_scope import current_secret_scope, get_secret, serves_routed_profile
    routed = serves_routed_profile()
    scope = (current_secret_scope() or {}) if routed else None
    values = {key: (scope.get(key) if routed else get_secret(key)) for key in _HARNESS_PASSTHROUGH_KEYS}
    return {key: value for key, value in values.items() if value is not None}


def _base_subprocess_env() -> dict:
    from tools.browser_session import build_browser_env
    env = build_browser_env()
    env.update(_harness_credentials())
    # The harness runs on Hermes's own interpreter, but a bundled Desktop install boots that
    # interpreter with its site dir on PYTHONPATH (no venv to activate), and the harness's daemon
    # re-runs sys.executable. Point PYTHONPATH at the harness's site dir, replacing whatever the
    # agent process inherited, so both the CLI and its daemon import the same packages.
    env.pop("PYTHONHOME", None)
    site_dir = _harness_site_dir()
    if site_dir:
        env["PYTHONPATH"] = site_dir
    else:
        env.pop("PYTHONPATH", None)
    env["PATH"] = _floor_subprocess_path(env.get("PATH", ""))
    env.setdefault("ANONYMIZED_TELEMETRY", "false")
    namespace_harness_dirs(env, _served_profile_tag())  # a served profile's own harness runtime/tmp dirs
    # A daemon the CLI starts inherits this tag; _own_harness_daemon owns only one that names us.
    env.update(spawn_env("browser-harness-daemon"))
    return env


def _floor_subprocess_path(path: str) -> str:
    """Keep system commands reachable from profile workers with a minimal PATH.

    Browser subprocesses may launch shell helpers; version-manager-only paths
    omit /usr/bin. Reuse the shared browser PATH floor on POSIX.
    """
    if os.name == "nt":
        return path
    parts = [p for p in (path or "").split(os.pathsep) if p]
    return os.pathsep.join(parts + [d for d in _FHS_BIN_DIRS if d not in set(parts) and os.path.isdir(d)])


def _read_browser_cfg() -> dict:
    """Return the ``browser:`` config section, or {} on any failure."""
    try:
        from tools.browser_session_cloud import browser_section
        return browser_section()
    except Exception as e:
        logger.debug("Could not read browser config section: %s", e)
        return {}


def _use_gateway(browser_cfg: dict) -> bool:
    """True when the browser section selects the Nous Tool Gateway — by the current ``hermes tools``
    picker row (``cloud_provider: nous``) or the pre-picker ``use_gateway: true`` flag. Reading only
    the legacy flag missed every picker-configured gateway, and the direct-API branch it fell into
    holds no credentials in managed mode (#108310)."""
    if is_truthy_value(browser_cfg.get("use_gateway"), default=False):
        return True
    try:
        from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER
    except Exception:  # pragma: no cover — helper ships with the package
        return False
    return str(browser_cfg.get("cloud_provider") or "").strip().lower() == NOUS_MANAGED_PROVIDER


def is_legacy_browser_use_cloud_config(browser_cfg: dict) -> bool:
    """True for pre-CLI direct-API Browser Use cloud configs: no cloud_provider (or ``browser-use``), not the
    Nous gateway, and BROWSER_USE_API_KEY set; the CLI then reaches Browser Use cloud natively."""
    if not isinstance(browser_cfg, dict):
        return False
    provider = str(browser_cfg.get("cloud_provider") or "").strip().lower()
    if provider not in {"browser-use", ""} or _use_gateway(browser_cfg):
        return False
    return _browser_use_api_key_set()


def _browser_use_api_key_set() -> bool:
    """BROWSER_USE_API_KEY for the served profile: the only credential Hermes autospawns BU cloud with.
    Profile-scoped so a multiplexed secondary never inherits the default's cloud mode."""
    from agent.secret_scope import get_secret
    return bool(get_secret("BROWSER_USE_API_KEY", ""))


def _harness_site_dir() -> Optional[str]:
    """The site dir Hermes's interpreter imports ``browser_harness`` from, or None."""
    spec = importlib.util.find_spec("browser_harness")
    if spec is None or not spec.origin:
        return None
    return str(Path(spec.origin).resolve().parent.parent)


def _find_cli() -> Optional[list[str]]:
    """The Browser Use CLI's engine (browser-harness) is a core dependency of Hermes's own venv,
    so every install, the Desktop bundle included, runs it on the current interpreter."""
    if _harness_site_dir() is None:
        return None
    return [sys.executable, "-m", "browser_harness.run"]


def _safe_task_key(task_id: Optional[str]) -> str:
    """Filesystem-safe key for a task id ("default" when none): workspace dir and tab-ledger owner."""
    return _TASK_ID_SAFE_RE.sub("_", str(task_id or "default"))[:80] or "default"


def browser_exec_available() -> bool:
    """check_fn: browser-harness is importable (a core dependency). Never launches or opens sockets."""
    return _find_cli() is not None


def _workspace_dir(task_id: Optional[str]) -> Optional[str]:
    """Stable per-task scratch dir that persists across browser_exec calls"""
    if os.environ.get("BH_AGENT_WORKSPACE"):
        return os.environ["BH_AGENT_WORKSPACE"]
    try:
        path = Path(get_hermes_home()) / "cache" / "browser-use" / "workspace" / _safe_task_key(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except Exception as e:
        logger.debug("browser_exec workspace unavailable: %s", e)
        return None


def _find_screenshot(stdout: str, since: float) -> Optional[str]:
    """Last screenshot path printed during this exec that exists and was written after
    the exec started, or None."""
    for path in reversed(_IMAGE_PATH_RE.findall(stdout or "")):
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= since - 1:
                return path
        except OSError:
            continue
    return None


def _native_screenshot_result(result: dict[str, Any], path: str) -> Optional[dict[str, Any]]:
    """Build a multimodal tool result attaching path for vision models"""
    try:
        from tools.vision_tools import (_EMBED_MAX_DIMENSION,
                                        _resize_image_for_vision, _should_use_native_vision_fast_path)
        from tools.vision_tools_history_budget import resolve_embed_target_bytes
        if not _should_use_native_vision_fast_path():
            return None
        # History-reuse cap: this data URL bakes into the tool result and is re-sent every later turn —
        # same policy as the vision_analyze native embed.
        data_url = _resize_image_for_vision(Path(path), mime_type="image/png",
                                            max_base64_bytes=resolve_embed_target_bytes(),
                                            max_dimension=_EMBED_MAX_DIMENSION, force_jpeg=True)
        text = json.dumps(result, ensure_ascii=False)
        attached = text + "\n\nThe screenshot from this call is attached — inspect it with your native vision."
        return {"_multimodal": True, "text_summary": text, "meta": {"screenshot_path": path, "native_vision": True},
                "content": [{"type": "text", "text": attached}, {"type": "image_url", "image_url": {"url": data_url}}]}
    except Exception as e:
        logger.debug("Native screenshot attach failed (falling back to text): %s", e)
        return None


def _served_profile_tag() -> str:
    """``""`` outside a served-profile scope (every legacy key stays byte-identical); under a
    multiplexed turn, the routed profile's home key — one profile's browser must never be handed
    to another that happens to use the same session name or task id (#110032)."""
    from hermes_constants import get_hermes_home_override, hermes_home_key
    return "" if get_hermes_home_override() is None else hermes_home_key()


def _backend_cache_key(task_id: Optional[str], session_name: str = "", sidecar: bool = False) -> str:
    """Session-cache key for a backend browser: named sessions get their own; served profiles get their own.
    ``sidecar`` appends the hybrid ``::local`` suffix, which makes ``_get_session_info`` build local Chromium
    even when a cloud provider is configured."""
    key = f"bu-named-{session_name}" if session_name else (task_id or "browser-exec-default")
    tag = _served_profile_tag()
    key = f"{key}@{tag}" if tag else key
    return f"{key}::local" if sidecar else key


def _sidecar_daemon_name(name: str) -> str:
    """BU_NAME of the harness daemon serving the hybrid local sidecar. The daemon reads BU_CDP_* once at
    spawn and outlives the call, so sharing the cloud route's daemon would relay a private URL to the
    cloud browser it is already attached to. User session names cannot start with ``_``."""
    return f"_local_{name or 'default'}"[:64]


def _export_real_profile_note(env: dict, key: str) -> None:
    """The session layer decides real profile vs fresh browser once per key; carry its note (if it fell
    back) so the result reports the browser this call actually drives."""
    from tools.browser_session import real_profile_note
    note = real_profile_note(key)
    if note:
        env.setdefault(_REAL_PROFILE_FALLBACK_SENTINEL, note)


def _mark_local_browser(env: dict, session: dict, key: str) -> None:
    """Flags for a LOCAL session browser: it runs on the Bot Desktop display (lease fence), and unless a
    persistent profile / the real-profile browser is shared by every key, it is exclusive to this key (no
    own-tab preamble: nothing to share a tab with)."""
    features = session.get("features") or {}
    env[_BOT_DESKTOP_BROWSER_SENTINEL] = "lightpanda" if features.get("lightpanda") else "local"
    if not session.get("shared_profile") and not features.get("real_profile"):
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    _export_real_profile_note(env, key)


def _resolve_local_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                       sidecar: bool = False) -> Optional[str]:
    """Local browser for this cache key through the session layer: the consented real profile, Lightpanda
    (``browser.engine: lightpanda``), or the packaged Chromium. Never the harness's own discovery of the
    user's installed Chrome (chrome://inspect toggle + Allow popup per run, blocked on Chrome >=136)."""
    from tools.browser_lightpanda import using_lightpanda_engine
    from tools.browser_session import get_session_info
    key = _backend_cache_key(task_id, session_name, sidecar)
    session: dict = {}

    def _session_info(cache_key: str) -> dict:
        session.update(get_session_info(cache_key) or {})
        return session

    if _quiet(using_lightpanda_engine, False):
        fail = lambda e: (f"Lightpanda could not be started: {e} Set browser.engine to auto "
                          "to use local Chrome, or switch backends via `hermes tools` → Browser Automation.")
    else:
        fail = lambda e: (f"The local browser could not be started: {e} "
                          "Run `hermes tools` → Browser Automation to (re)install Chromium, or switch backends.")
    err = _export_session_cdp(env, _session_info, key, fail, "The local browser session returned no CDP endpoint.")
    if err is None:
        _mark_local_browser(env, session, key)
    return err


def _resolve_backend_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                         force_local_sidecar: bool = False) -> Optional[str]:
    """Point the harness at the configured backend's CDP endpoint; error string on failure.

    Precedence: (1) ``BU_CDP_WS``/``BU_CDP_URL`` already in env (operator override); (2) ``BROWSER_CDP_URL``
    env / ``browser.cdp_url`` (``/browser connect``); (3) a cloud provider via ``browser_session.get_session_info``
    (per-key cache, expiry, reaper, atexit; a provider failure falls back to a local browser);
    (4) the local engine — ``browser.engine: lightpanda`` or Hermes' packaged Chromium via the launcher
    (never the harness's own discovery of the user's installed Chrome); (5) BU direct-API configs WITH
    ``BROWSER_USE_API_KEY`` → None (checked before (3)): the CLI reaches BU cloud natively (BU_AUTOSPAWN); a
    keyless ``browser-use`` provider (managed-gateway autodetect, or a selection missing its key) takes (3).
    ``session_name`` (BU_NAME) keys the session cache so each name gets its OWN browser — what makes named
    sessions concurrent-safe.

    ``force_local_sidecar`` skips step (3): the call runs on the local engine under the ``::local`` cache
    key (hybrid routing; see ``browser_url_policy.hybrid_routes_locally``). It sits BELOW the two operator overrides, and the
    sidecar verdict never fires while a CDP override is set."""
    if _has_cdp_env(env):
        return None
    from tools.browser_session import get_session_info
    from tools.browser_session_cdp import get_cdp_override
    from tools.browser_session_cloud import get_cloud_provider
    override = _quiet(get_cdp_override, "")
    if override:
        _set_cdp_env(env, override)
        return None
    if force_local_sidecar:
        return _resolve_local_cdp(env, task_id, session_name, sidecar=True)
    provider = _quiet(get_cloud_provider, None, "Cloud provider lookup failed")
    if provider is None:
        return _resolve_local_cdp(env, task_id, session_name)

    # Browser Use direct-API configs: the CLI talks to BU cloud natively (BU_AUTOSPAWN) — the
    # legacy provider would create a second, redundant session. Nous-gateway configs (cloud_provider: nous
    # from the picker, or the pre-picker use_gateway: true) DO resolve through the provider: the gateway
    # provisions the browser server-side and returns its CDP URL. Without BROWSER_USE_API_KEY the CLI cannot
    # autospawn, and the provider is either the managed gateway (autodetect on a never-configured install) or
    # a selection missing its key; both go through the session path (gateway CDP, or the session layer's
    # local-Chromium fallback) instead of leaving the harness to hunt for an installed Chrome.
    provider_key = str(getattr(provider, "name", "") or "").strip().lower()
    if provider_key == _BROWSER_USE_PROVIDER and not _use_gateway(_read_browser_cfg()) and _browser_use_api_key_set():
        env[_PRIVATE_BROWSER_SENTINEL] = "1"  # named BU cloud browsers are exclusive to their daemon
        # Only intent: the harness attaches to a local Chrome already listening instead of autospawning, so
        # the live daemon's own report decides.
        from tools.browser_use_shot import ACTIVATE_IF_CLOUD
        env[_CLOUD_BROWSER_SENTINEL] = ACTIVATE_IF_CLOUD
        return None

    provider_name = type(provider).__name__
    session: dict = {}

    def _session_info(key: str) -> dict:
        session.update(get_session_info(key) or {})
        return session

    key = _backend_cache_key(task_id, session_name)
    err = _export_session_cdp(
        env, _session_info, key,
        lambda e: (f"Cloud browser provider {provider_name} failed to provide a session: {e}. "
                   "Fix the provider configuration or switch backends via `hermes tools` → Browser Automation."),
        f"Cloud browser provider {provider_name} returned no CDP endpoint, so browser_exec "
        "cannot drive it. Pick a CDP-capable provider via `hermes tools` → Browser Automation.",
    )
    if session.get("fallback_from_cloud"):
        # The provider failed (rate limit, quota, outage) and the session layer fell back to a LOCAL
        # browser for this key; it already carries its CDP URL.
        logger.warning("browser_exec: %s failed (%s); using local Chromium for this session",
                       provider_name, session.get("fallback_reason"))
        if err is None:
            _mark_local_browser(env, session, key)
        return err
    if err is None:
        from tools.browser_use_shot import ACTIVATE_ALWAYS
        env[_CLOUD_BROWSER_SENTINEL] = ACTIVATE_ALWAYS  # the provider's own session: the harness connects only there
    # A provider browser keyed bu-named-<name> is exclusive to this session — the
    # own-tab preamble would just leak a blank tab into it.
    if err is None and session_name:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    return err


def _resolve_real_profile_cdp(env: dict, force_local: bool) -> Optional[str]:
    """Point the harness at the user's real-profile copy-browser (a SNAPSHOT of their default Chromium
    profile, hermes_cli.browser_connect) when consented. Two ways in: the effective backend is already local
    (no provider, CDP override, or legacy BU cloud config) → silent upgrade; or ``force_local`` (consent-gated
    ``local`` arg) → the user's browser even under a cloud backend. Operator overrides (BU_CDP_* env,
    /browser connect, ``browser.cdp_url``) own the session either way. A launch error is returned;
    ``_route_backend`` then falls back to the normal backend and reports it, never silently."""
    if not _real_profile_consented() or _has_cdp_env(env):
        return None
    from tools.browser_session_cdp import get_cdp_override_raw
    from tools.browser_session_cloud import get_cloud_provider
    from tools.browser_session_real_profile import real_profile_cdp
    if _quiet(get_cdp_override_raw, ""):
        return None
    # Only auto-upgrade genuinely-local attaches; any cloud path (provider, provider lookup failure, or
    # legacy BU cloud config) stays on its backend unless the model passes local=true.
    if not force_local and (_quiet(get_cloud_provider, object()) is not None
                            or is_legacy_browser_use_cloud_config(_read_browser_cfg())):
        return None
    cdp, err = real_profile_cdp()
    if cdp and not err:
        _set_cdp_env(env, cdp)
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "local"  # the user's real Chromium, whatever browser.engine says
    return err or None


def _attach_vault_supervisor(env: dict, task_id: Optional[str]) -> None:
    """Attach the per-task CDP supervisor to the browser this exec drives so ``browser_vault`` fill has
    a secret-capable WebSocket (never argv) into the SAME browser. Only CDP-routed backends expose an
    endpoint; BU direct-cloud (BU_AUTOSPAWN) does not, and the vault tools report ``supervisor_required``."""
    cdp = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    if not cdp:
        return
    try:
        from tools.browser_session_cdp import get_dialog_policy_config, resolve_cdp_override
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        from tools.browser_agent_lifecycle import supervisor_identity
        policy, timeout_s = get_dialog_policy_config()
        owner = task_id or "default"
        name = env.get("BU_NAME", "")
        if name.startswith("lease-") and cdp.endswith("/lease/" + name[6:]):
            owner = supervisor_identity(owner, name[6:])  # [daveey] managed lease: per-lease supervisor
        SUPERVISOR_REGISTRY.get_or_start(task_id=owner, cdp_url=resolve_cdp_override(cdp),
                                         dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("browser_exec: CDP supervisor attach failed (non-fatal): %s", exc)


def _route_backend(env: dict, session: str, task_id: Optional[str], local: bool,
                   force_local_sidecar: bool = False, real_profile: bool = True) -> Optional[str]:
    """Resolve where the harness connects; returns an error string or None. Real-profile consent runs
    BEFORE provider resolution so a hit short-circuits the cloud path via the BU_CDP_* env contract. Named
    sessions compose with the backend: BU_NAME namespaces the harness daemon (IPC socket, log, pid) and on
    provider backends additionally keys its own cloud browser.

    ``force_local_sidecar`` (a call carrying a private URL) declines the cloud provider for the hybrid local
    sidecar; unlike ``local`` (the consent-gated real-profile browser) it never touches the user's profile."""
    # ``real_profile=False``: this route already fell back, so it stays on its fresh browser instead
    # of swapping browsers mid-task once the profile frees up.
    rp_err = _resolve_real_profile_cdp(env, force_local=local) if real_profile else None
    # local=True is only served by the real-profile route; consent off must not pretend.
    if local and not _has_cdp_env(env) and not _real_profile_consented():
        return ("local=true was requested but browser.use_real_profile is off. Enable it in config.yaml "
                "(browser.use_real_profile: true) or the desktop Settings → Browser section, then retry.")
    # local=True promised the user's own machine: a profile that can't load runs on a fresh LOCAL browser
    # (the sidecar, which never loads the profile), never the cloud one. Without ``local`` the session
    # layer makes the one real-profile-or-fresh decision and reports it (_resolve_local_cdp).
    if local and not _has_cdp_env(env) and (rp_err or not real_profile):
        if rp_err:
            from tools.browser_session_real_profile import real_profile_fallback_note
            env[_REAL_PROFILE_FALLBACK_SENTINEL] = real_profile_fallback_note(rp_err)
        env[_LOCAL_FALLBACK_SENTINEL] = "1"
        force_local_sidecar = True
    return _resolve_backend_cdp(env, task_id, session_name=session,
                                force_local_sidecar=force_local_sidecar)


def _group_popen_kwargs() -> dict:
    """Popen kwargs starting the CLI in its own process group (a new session on POSIX) so a
    timeout can take down every process that inherited the capture pipes, not just the CLI
    child. Windows also hides the console window."""
    def _flags() -> dict:
        from hermes_cli._subprocess_compat import windows_hide_flags
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"creationflags": windows_hide_flags() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                "startupinfo": si}
    # detached-spawn: ok — awaited CLI; a timeout kills its whole group (_run_cli_killing_process_group)
    return _quiet(_flags, {}, "Windows hide-flags unavailable") if os.name == "nt" else {"start_new_session": True}


def _clamp_timeout(timeout_s: Any) -> int:
    try:
        return max(_MIN_TIMEOUT_S, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


# After a whole-group SIGKILL, every pipe holder is dead, so the drain below is normally
# instant; the deadline only guards against a process outside the group still holding a pipe.
_POST_KILL_DRAIN_S = 10.0


def _kill_cli_process_group(proc) -> None:
    """SIGKILL the CLI's whole process group (POSIX; ``start_new_session`` made pgid == pid) or,
    on Windows, its process tree via ``taskkill /T /F`` — the only group-wide kill it offers."""
    if os.name == "nt":
        from hermes_cli._subprocess_compat import windows_hide_flags
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                           check=False, creationflags=windows_hide_flags())
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX only, the nt branch returned above


def _run_cli_killing_process_group(cmd, code, env, timeout):
    """Run the CLI in its own process group and kill the whole group on timeout.

    ``subprocess.run`` only kills the direct child on ``TimeoutExpired``; a grandchild that
    inherited the stdout/stderr pipes (browser_harness daemon / Chrome helper) is orphaned
    still holding them, and on Windows ``run()``'s unbounded post-kill ``communicate()`` then
    blocks on pipe EOF forever — so the tool call, plus its activity heartbeat, wedges (#106244).
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, **_group_popen_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(input=code, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_cli_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_POST_KILL_DRAIN_S)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# How often a running browser_exec re-checks for the daemon its first call starts.
_OWN_DURING_CALL_S = 0.5
# Harness daemons this process drove: each latches BU_CDP_* at start and outlives every call, so a backend
# swap (/browser connect|disconnect) leaves later calls in the old browser until these are stopped.
_driven_daemons: dict[tuple[str, ...], str] = {}  # daemon identity -> home
_driven_daemons_lock = threading.Lock()
_live_generation = functools.partial(_lazy_call, "tools.browser_session_cdp", "live_cdp_generation", 0,
                                     "live CDP generation lookup failed")  # this profile's publish count
# Route cache key -> whether its last browser_exec ran on the hybrid local sidecar.
_exec_route_is_sidecar: dict[str, bool] = {}
# Route cache key -> the note from its real-profile fallback; that route stays on the fallback browser.
_real_profile_fallback: dict[str, str] = {}
_real_profile_note_delivered: set = set()


def stop_harness_daemons(home_key: Optional[str] = None) -> None:
    """Stop every harness daemon this process drove (only ``home_key``'s when given), through the
    harness's own identity-checked ``--reload``; the next browser_exec respawns one on the endpoint
    it resolves then."""
    with _driven_daemons_lock:
        daemons = sorted(key for key, home in _driven_daemons.items() if home_key in (None, home))
        for key in daemons:
            _driven_daemons.pop(key, None)
    _reload_daemons(daemons)


def _published_daemon_pid(env: dict) -> Optional[int]:
    """The pid the harness published for the daemon ``env`` drives (``harness_pid_file``: JSON
    ``{"pid", "started"}`` or a bare pid), or None when there is none."""
    try:
        raw = harness_pid_file(env).read_text(encoding="utf-8-sig").strip()
        return int(json.loads(raw)["pid"]) if raw.startswith("{") else int(raw.split()[0])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _own_harness_daemon_until_owned(env: dict) -> Optional[bool]:
    """Periodic check while browser_exec runs: own the daemon as soon as it publishes its pid, then
    stop (False). It also stops once the daemon can never be ours (Windows, a managed worker, a daemon
    another Hermes started), instead of re-checking every tick for the whole call."""
    return None if _own_harness_daemon(env) is None else False


def _reload_daemons(daemons) -> None:  # each ``daemon_identity`` daemon, with its own dirs
    cmd = _find_cli() if daemons else None
    if not cmd:
        return
    env = _base_subprocess_env()
    for daemon in daemons:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            _run_cli_killing_process_group([*cmd, "--reload"], "", daemon_env(env, daemon), 15)
        with _owned_daemon_groups_lock:
            pgid = _owned_daemon_groups.pop(daemon, None)
        if pgid is not None:
            release_process_group_once_empty(pgid)  # a daemon --reload failed to stop stays covered


# Daemon identity (``daemon_identity``: its dirs + BU_NAME) -> process group of the harness daemon this
# process owns. The harness starts its daemon with start_new_session so it outlives each CLI call (that
# is how a session persists across browser_exec calls), which also means it outlives a SIGKILLed or
# OOM-killed Hermes forever (#121095). Its group goes to this process's owner-death supervisor, so it
# dies with us however we die.
_owned_daemon_groups: dict[tuple[str, ...], int] = {}
_owned_daemon_groups_lock = threading.Lock()


def _own_harness_daemon(env: dict) -> Optional[bool]:
    """Hand the daemon behind ``env``'s BU_NAME to the owner-death supervisor.

    The harness publishes its daemon pid in its runtime dir (``harness_pid_file``: JSON
    ``{"pid", "started"}`` or a bare pid). The daemon is accepted only while it is this user's live
    ``browser_harness.daemon`` leading its own session and its spawn tag names this process, so a
    stale or planted pid file, or a daemon another Hermes started, never reaches the supervisor.
    Runs after every browser_exec (and while a long one runs, until the daemon is owned): a pid
    already owned for this daemon costs one file read.

    Returns True once the daemon is owned, False when it can never be (Windows, a managed worker, a
    daemon another Hermes started), None when there is nothing to own yet."""
    if os.name != "posix":
        return False  # Windows: the harness daemon is outside this process's job; nothing to register.
    from agent.runtime_session_store import is_worker_process
    if is_worker_process():
        # A managed per-turn worker never owns it, so a killed worker does not take the browser the
        # next turn's worker drives along (its clean exit still runs the atexit browser cleanup).
        return False
    import psutil

    daemon = daemon_identity(env)
    pid = _published_daemon_pid(env)
    if pid is None:
        logger.debug("browser_exec: no harness daemon pid published for %s", daemon[-1])
        return None
    try:
        with _owned_daemon_groups_lock:
            if _owned_daemon_groups.get(daemon) == pid:
                return True
        proc = psutil.Process(pid)
        argv = proc.cmdline()
        if (os.getsid(pid) != pid or proc.uids().real != os.getuid()  # windows-footgun: ok — posix-only, returned above
                or argv[argv.index("-m") + 1] != "browser_harness.daemon"):
            return None
        if not spawned_by_this_process(pid):
            return False  # another Hermes on the same BU_NAME started it: that Hermes owns it
    except (OSError, ValueError, IndexError, psutil.Error) as exc:
        logger.debug("browser_exec: no harness daemon to supervise for %s: %s", daemon[-1], exc)
        return None
    with _owned_daemon_groups_lock:
        replaced = _owned_daemon_groups.get(daemon)
        _owned_daemon_groups[daemon] = pid
    own_process_groups([pid])
    register_child(pid, "browser-harness-daemon")
    if replaced is not None:
        release_process_group_once_empty(replaced, timeout=0)  # the daemon it replaced; a survivor stays covered
    return True


def _route_exec(env: dict, session: str, task_id: Optional[str], local: bool, local_sidecar: bool,
                route_key: str) -> tuple[Optional[str], bool]:
    """``_route_backend`` for one browser_exec call; returns ``(error, runs on the local sidecar)``.
    A real-profile fallback is remembered per route: the route keeps its fresh browser and the note
    waits until a result carries it (``_take_real_profile_note``)."""
    route_err = _route_backend(env, session, task_id, local, force_local_sidecar=local_sidecar,
                               real_profile=route_key not in _real_profile_fallback)
    note = env.pop(_REAL_PROFILE_FALLBACK_SENTINEL, None)
    if note:
        _real_profile_fallback.setdefault(route_key, note)
    return route_err, local_sidecar or bool(env.pop(_LOCAL_FALLBACK_SENTINEL, None))


def _route_exec_current(env: dict, session: str, task_id: Optional[str], local: bool,
                        local_sidecar: bool, route_key: str) -> tuple[dict, Optional[str], bool, int]:
    """``_route_exec`` on a copy of ``env`` -> ``(routed env, error, sidecar, generation)``, re-routed (at
    most 3 tries) when /browser connect published meanwhile; ``_reload_if_republished`` covers dispatch."""
    for _attempt in range(3):
        routed = dict(env)
        generation = _live_generation()
        route_err, sidecar = _route_exec(routed, session, task_id, local, local_sidecar, route_key)
        if route_err or _live_generation() == generation:
            break
    return routed, route_err, sidecar, generation


def _reload_if_republished(env: dict, generation: int) -> None:
    """Stop this call's daemon if the profile published a new endpoint while it spawned on the old one."""
    if _live_generation() == generation:
        return
    daemon = daemon_identity(env)
    with _driven_daemons_lock:
        _driven_daemons.pop(daemon, None)
    _reload_daemons([daemon])


def _take_real_profile_note(route_key: str) -> dict:
    """The fallback note for the first result this route returns after falling back, then nothing."""
    if route_key not in _real_profile_fallback or route_key in _real_profile_note_delivered:
        return {}
    _real_profile_note_delivered.add(route_key)
    return {"real_profile_unavailable": _real_profile_fallback[route_key]}


# [daveey] Named harness daemons outlive their owner: the CLI spawns them with start_new_session, they
# have no idle exit, and an owner that is SIGKILLed (Kanban scope kill, Desktop serve child) or takes the
# CDP-override route (no _active_sessions entry, so the atexit cleanup is skipped) never stops them. Each
# browser_exec on a named daemon stamps <harness runtime>/hermes-owners/<BU_NAME>/<pid>-<start>; the reaper
# stops a named daemon once no stamp names a live process, or once no owner has driven it for the idle
# window. The shared "default" daemon is never stamped or reaped.
_OWNERS_DIR = "hermes-owners"
_DEFAULT_DAEMON_IDLE_S = 7200
_REAP_INTERVAL_S = 60
_reap_lock = threading.Lock()
_last_reap = 0.0


def _harness_runtime_dir(env: dict) -> Path:
    """The harness's runtime dir (sock/pid files) as its daemon will resolve it from ``env``
    (browser_harness.paths.runtime_dir)."""
    if env.get("BH_RUNTIME_DIR"):
        return Path(env["BH_RUNTIME_DIR"]).expanduser()
    home = env.get("BH_HOME") or env.get("BROWSER_HARNESS_HOME")
    if home:
        return Path(home).expanduser() / "runtime"
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]).expanduser() / "browser-harness" / "runtime"
    base = Path(env["HOME"]).expanduser() if env.get("HOME") else Path.home()
    return base / ".config" / "browser-harness" / "runtime"


def _own_start() -> Optional[int]:
    from gateway.status import get_process_start_time
    return _quiet(lambda: get_process_start_time(os.getpid()), None, "process start time unavailable")


def _owner_alive(stamp: str) -> bool:
    """``<pid>-<start>``: True while that exact process generation runs (a recycled pid has another start)."""
    from gateway.status import _pid_exists, get_process_start_time
    pid_s, _, start_s = stamp.partition("-")
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    # gateway.status._pid_exists, not os.kill(pid, 0): on Windows that signal is
    # CTRL_C_EVENT for the whole console group (bpo-14484). Zombies count as dead.
    if not _pid_exists(pid):
        return False
    if not start_s or start_s == "x":
        return True
    current = _quiet(lambda: get_process_start_time(pid), None, "process start time unavailable")
    return current is None or str(current) == start_s


def record_daemon_owner(env: dict, name: str) -> None:
    """Stamp this process as an owner of the named daemon (refreshing its last-use time)."""
    if not name or name == "default":
        return
    try:
        owners = _harness_runtime_dir(env) / _OWNERS_DIR / name
        owners.mkdir(parents=True, exist_ok=True)
        start = _own_start()
        (owners / f"{os.getpid()}-{start if start is not None else 'x'}").touch()
    except OSError as e:
        logger.debug("harness owner stamp for %s failed: %s", name, e)


def _browser_cfg_int(key: str, default: int) -> int:
    try:
        return int(_read_browser_cfg().get(key, default))
    except (TypeError, ValueError):
        return default


def _daemon_idle_s() -> int:
    return _browser_cfg_int("harness_daemon_idle_s", _DEFAULT_DAEMON_IDLE_S)


def reap_orphaned_harness_daemons(env: Optional[dict] = None, *, drop_own: bool = False,
                                  now: Optional[float] = None) -> list[str]:
    """Stop every stamped named daemon with no live owner or idle past ``browser.harness_daemon_idle_s``.

    ``drop_own`` (process exit) first removes this process's stamps, so a daemon only this process drove
    is stopped while one a live sibling still drives is kept. Returns the BU_NAMEs stopped."""
    env = env if env is not None else _base_subprocess_env()
    now = time.time() if now is None else now
    _reap_idle_agent_tabs(env, now)
    root = _harness_runtime_dir(env) / _OWNERS_DIR
    if not root.is_dir():
        return []
    idle_s = _daemon_idle_s()
    me = f"{os.getpid()}-"
    stale: list[str] = []
    for owners in sorted(p for p in root.iterdir() if p.is_dir()):
        live_mtimes = []
        for stamp in list(owners.iterdir()):
            if (drop_own and stamp.name.startswith(me)) or not _owner_alive(stamp.name):
                with contextlib.suppress(OSError):
                    stamp.unlink()
                continue
            with contextlib.suppress(OSError):
                live_mtimes.append(stamp.stat().st_mtime)
        if not live_mtimes or (idle_s > 0 and now - max(live_mtimes) > idle_s):
            stale.append(owners.name)
    cmd = _find_cli() if stale else None
    stopped = []
    for name in stale:
        if cmd:
            try:
                _run_cli_killing_process_group([*cmd, "--reload"], "", {**env, "BU_NAME": name}, 15)
            except (OSError, subprocess.SubprocessError) as e:
                logger.debug("harness daemon %s stop failed: %s", name, e)
                continue
        with _driven_daemons_lock:
            _driven_daemons.pop(daemon_identity({**env, "BU_NAME": name}), None)
        owners = root / name
        for stamp in list(owners.iterdir()) if owners.is_dir() else []:
            with contextlib.suppress(OSError):
                stamp.unlink()
        with contextlib.suppress(OSError):
            owners.rmdir()
        stopped.append(name)
    if stopped:
        logger.info("Stopped %d orphaned/idle browser-harness daemon(s): %s", len(stopped), ", ".join(stopped))
    return stopped


def _maybe_reap_harness_daemons(env: dict) -> None:
    """At most once per ``_REAP_INTERVAL_S`` per process, from browser_exec."""
    global _last_reap
    with _reap_lock:
        if time.time() - _last_reap < _REAP_INTERVAL_S:
            return
        _last_reap = time.time()
    try:
        reap_orphaned_harness_daemons(env)
    except Exception as e:
        logger.debug("harness daemon reap failed: %s", e)


# [daveey] Agent tab ledger (see _TAB_LEDGER_PREAMBLE): <harness runtime>/hermes-agent-tabs/<owner>.json =
# {"owner", "last_used", "tabs": {target_id: {"created", "bu_name", "url"}}}, owner = the safe task id.
_TAB_LEDGER_DIR = "hermes-agent-tabs"
_DEFAULT_TAB_CAP_PER_OWNER = 3
_DEFAULT_TAB_CAP = 8
_DEFAULT_TAB_IDLE_S = 1800
# One exec per daemon closes a batch; prints the ids that were open so the caller can report them.
_CLOSE_TABS_CODE = """\
# hermes: close this task's agent-opened tabs
_live = {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]}
for _tid in %r:
    if _tid in _live:
        cdp("Target.closeTarget", targetId=_tid)
        print("hermes-closed " + _tid)
"""


def _tab_ledger_path(env: dict, owner: Optional[str]) -> Path:
    return _harness_runtime_dir(env) / _TAB_LEDGER_DIR / f"{_safe_task_key(owner)}.json"


def _read_tab_ledger(path: Path) -> dict:
    """The owner's ledger; a missing or corrupt file is an empty one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("tabs"), dict):
        return {"owner": path.stem, "last_used": 0, "tabs": {}}
    return data


def _write_tab_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def touch_tab_owner(env: dict, owner: Optional[str]) -> None:
    """Refresh the owner's last_used (every browser_exec): the global cap and the reaper only take tabs
    from owners idle past their window."""
    path = _tab_ledger_path(env, owner)
    try:
        data = _read_tab_ledger(path)
        data.update(owner=path.stem, last_used=time.time())
        _write_tab_ledger(path, data)
    except OSError as e:
        logger.debug("tab ledger touch for %s failed: %s", path.stem, e)


def _close_tabs_via_daemon(cmd: list, env: dict, name: str, ids: list) -> tuple[bool, list[str]]:
    """Close ``ids`` through the RUNNING daemon ``name`` (never spawns one: a fresh CDP connection can pop
    Chrome's Allow prompt). Returns (daemon reachable, ids closed)."""
    run_env = {**env, "BU_NAME": name, "BH_REQUIRE_EXISTING_DAEMON": "1"}
    try:
        proc = _run_cli_killing_process_group(cmd, _CLOSE_TABS_CODE % (ids,), run_env, 30)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("closing agent tabs via %s failed: %s", name, e)
        return False, []
    closed = [ln.split(" ", 1)[1] for ln in (proc.stdout or "").splitlines() if ln.startswith("hermes-closed ")]
    return proc.returncode == 0, [t for t in closed if t in ids]


def close_agent_tabs(owner: Optional[str], env: Optional[dict] = None) -> list[str]:
    """Close every tab in ``owner``'s ledger through the daemon that recorded it (falling back to the
    default daemon when that one is gone; target ids are browser-wide), then delete the ledger file. Tabs
    already gone are ignored, and only ledger ids are ever closed. Returns the ids closed."""
    env = env if env is not None else _base_subprocess_env()
    path = _tab_ledger_path(env, owner)
    tabs = _read_tab_ledger(path)["tabs"]
    cmd = _find_cli() if tabs else None
    if tabs and not cmd:
        return []  # no harness to close them with: keep the ledger for a later sweep
    by_daemon: dict[str, list[str]] = {}
    for tid, info in tabs.items():
        name = info.get("bu_name") if isinstance(info, dict) else None
        by_daemon.setdefault(str(name or "default"), []).append(tid)
    closed: list[str] = []
    retry: list[str] = []
    for name in [n for n in sorted(by_daemon) if n != "default"] + ["default"]:
        ids = by_daemon.get(name, []) + (retry if name == "default" else [])
        if not ids:
            continue
        reachable, done = _close_tabs_via_daemon(cmd, env, name, ids)
        closed += done
        if not reachable and name != "default":
            retry += [t for t in ids if t not in done]
    with contextlib.suppress(OSError):
        path.unlink()
    if closed:
        logger.info("Closed %d agent tab(s) for %s", len(closed), path.stem)
    return closed


def _reap_idle_agent_tabs(env: dict, now: float) -> list[str]:
    """Close the tabs of every ledger owner idle past ``browser.agent_tab_idle_s`` (a task that ended
    without cleanup_browser: SIGKILLed worker, crashed gateway). Returns the owners swept."""
    root = _harness_runtime_dir(env) / _TAB_LEDGER_DIR
    idle_s = _browser_cfg_int("agent_tab_idle_s", _DEFAULT_TAB_IDLE_S)
    if idle_s <= 0 or not root.is_dir():
        return []
    swept = []
    for path in sorted(root.glob("*.json")):
        if now - float(_read_tab_ledger(path).get("last_used") or 0) > idle_s:
            close_agent_tabs(path.stem, env)
            swept.append(path.stem)
    return swept


def _prepend_tab_preambles(code: str, env: dict, session: str, task_id: Optional[str]) -> str:
    """SHARED browser only (private and managed browsers skip this): the tab ledger for every session,
    then the own-tab pin for named ones. The ledger wraps cdp first, so the pin's createTarget is
    recorded. A cloud-admin call must keep its leading start/stop_remote_daemon( (run.py sniffs it)."""
    if code.lstrip().startswith(("start_remote_daemon(", "stop_remote_daemon(")):
        return code
    env["HERMES_AGENT_TAB_LEDGER"] = str(_tab_ledger_path(env, task_id))
    env["HERMES_AGENT_TAB_CAP_OWNER"] = str(_browser_cfg_int("agent_tab_cap_per_owner", _DEFAULT_TAB_CAP_PER_OWNER))
    env["HERMES_AGENT_TAB_CAP"] = str(_browser_cfg_int("agent_tab_cap", _DEFAULT_TAB_CAP))
    touch_tab_owner(env, task_id)
    return _TAB_LEDGER_PREAMBLE + (_OWN_TAB_PREAMBLE if session else "") + code


# [daveey] _resolve_exec_route hands the routed sidecar flag and endpoint generation back through env.
_ROUTED_SIDECAR_KEY = "_HERMES_BU_ROUTED_SIDECAR"
_ROUTED_GENERATION_KEY = "_HERMES_BU_ROUTED_GENERATION"


def _resolve_exec_route(env: dict, session: str, task_id: Optional[str], local: bool, local_sidecar: bool,
                        route_key: str = "") -> tuple[Optional[Any], Optional[str]]:
    """``(managed route, error)`` for one call; routes ``env`` in place. [daveey] A matching
    ``browser.managed_agent`` route skips backend discovery (an idle browser has no /json/version yet; the
    lease supplies the endpoint at dispatch); otherwise upstream's generation-checked ``_route_exec_current``."""
    from tools import browser_agent_lifecycle as managed_browser
    try:
        managed = managed_browser.configured_route(_read_browser_cfg(), env)
    except managed_browser.ManagedBrowserError as exc:
        return None, str(exc)
    if managed:
        return managed, None
    routed, route_err, sidecar, generation = _route_exec_current(
        env, session, task_id, bool(local), local_sidecar, route_key)
    env.clear()
    env.update(routed)
    env[_ROUTED_SIDECAR_KEY], env[_ROUTED_GENERATION_KEY] = sidecar, generation
    return None, route_err


def _set_exec_session(env: dict, session: str) -> Optional[str]:
    """Validate and export an explicit harness session name. [daveey]"""
    if session:
        if not _SESSION_RE.match(session):
            return (f"Invalid session name {session!r}: use 1-64 letters, digits, "
                    "dashes, or underscores (e.g. 'r7k2').")
        env["BU_NAME"] = session
    return None


def browser_exec(code: str, session: str = "", timeout_s: int = _DEFAULT_TIMEOUT_S,
                 task_id: Optional[str] = None, local: bool = False):
    """Run Python code through the browser-use CLI, and return its output"""
    from agent.redact import redact_sensitive_text
    from tools.registry import tool_error, tool_result
    if not code or not code.strip():
        return tool_error("No code provided. Pass Python that uses the pre-imported helpers, e.g. new_tab(\"https://example.com\") then print(page_info()).")

    # The verdict is carried into the route, never re-derived: the predicate resolves DNS, so a second
    # evaluation could relax the floor and still pick the cloud for a rebinding host.
    verdict = _code_url_verdict(code)
    if verdict.blocked:
        return tool_error(verdict.blocked)
    # One endpoint per call: any private URL sends the whole call to the local sidecar. A call with no URL
    # literal stays on the route the previous call took.
    route_key = _backend_cache_key(task_id, session)
    local_sidecar = verdict.local_sidecar if _URL_RE.search(code) else _exec_route_is_sidecar.get(route_key, False)

    cmd = _find_cli()
    if not cmd:
        return tool_error("browser-harness is missing from Hermes's Python environment. "
                          "Run `hermes update` to re-sync it.")

    env = _base_subprocess_env()
    session_error = _set_exec_session(env, session)
    if session_error:
        return tool_error(session_error)
    from tools import browser_agent_lifecycle as managed_browser
    generation = _live_generation()
    managed, route_err = _resolve_exec_route(env, session, task_id, local, local_sidecar, route_key)
    local_sidecar = bool(env.pop(_ROUTED_SIDECAR_KEY, local_sidecar))
    generation = env.pop(_ROUTED_GENERATION_KEY, generation)
    if route_err:
        return tool_error(route_err)
    if local_sidecar:
        env["BU_NAME"] = _sidecar_daemon_name(session)
    fit_default_socket_dir(env)  # BU_NAME is final: a deep HOME's socket would overflow sun_path
    _exec_route_is_sidecar[route_key] = local_sidecar
    local_engine = env.pop(_BOT_DESKTOP_BROWSER_SENTINEL, None)
    bot_desktop_browser = bool(local_engine)
    _exec_local_engine.set(local_engine)  # only local browsers carry the sentinel

    # SHARED browser (/browser connect CDP override): record agent-created tabs ([daveey] tab ledger) and pin
    # each named session to its own tab (see _OWN_TAB_PREAMBLE). Private per-name browsers skip this.
    private_browser = env.pop(_PRIVATE_BROWSER_SENTINEL, None)  # always pop: never exported to the CLI
    if not private_browser and not managed:
        code = _prepend_tab_preambles(code, env, session, task_id)  # [daveey] tab ledger + own-tab pin
    # Every backend: capture_screenshot() falls back to jpeg instead of hanging on a png a hidden tab never
    # renders; only on a cloud browser does it (and new_tab) bring the tab to the front (tools/browser_use_shot.py).
    from tools.browser_use_shot import ACTIVATE_NEVER, harness_command
    cmd = harness_command(cmd, activate=env.pop(_CLOUD_BROWSER_SENTINEL, None) or ACTIVATE_NEVER)

    workspace = _workspace_dir(task_id)
    if workspace:
        env["BH_AGENT_WORKSPACE"] = workspace

    # BU_AUTOSPAWN makes the CLI start a Browser Use cloud browser when no local
    # Chrome/CDP endpoint is reachable (their API key authenticates it)
    if "BU_AUTOSPAWN" not in env and is_legacy_browser_use_cloud_config(_read_browser_cfg()):
        env["BU_AUTOSPAWN"] = "1"

    timeout = _clamp_timeout(timeout_s)
    started = time.time()

    def dispatch() -> dict[str, Any]:
        owning = None
        try:
            lease = (managed_browser.request_lease(managed, task_id, session, timeout, env)
                     if managed else contextlib.nullcontext())
            with lease:
                _attach_vault_supervisor(env, task_id)
                if not managed:
                    with _driven_daemons_lock:
                        _driven_daemons[daemon_identity(env)] = hermes_home_key()
                    # [daveey] owner stamp + periodic orphan/idle daemon reap
                    record_daemon_owner(env, env.get("BU_NAME", "default"))
                    _maybe_reap_harness_daemons(env)
                    # The first call starts the daemon and may run long (navigation, user code): own it as soon
                    # as it publishes its pid, so a Hermes killed mid-call takes it along (the call's end owns
                    # it too).
                    from agent.periodic_scheduler import schedule
                    owning = schedule(lambda: _own_harness_daemon_until_owned(env), _OWN_DURING_CALL_S)
                return {"proc": _run_cli_killing_process_group(cmd, code, env, timeout)}
        except managed_browser.ManagedBrowserError as exc:
            return {"error_result": tool_error(str(exc))}
        except subprocess.TimeoutExpired:
            return {"error_result": tool_error(
                f"browser-use exec timed out after {timeout}s. The daemon may still be working; retry "
                f"with a larger timeout_s (max {_MAX_TIMEOUT_S}), or split the work into several calls that "
                "append to workspace files — anything already written to the workspace is preserved."
            )}
        except OSError as e:
            return {"error_result": tool_error(f"Failed to launch browser-use CLI: {e}")}
        finally:
            if owning is not None:
                owning.cancel(wait=5)  # an in-flight check finishes before the post-call one runs

    if bot_desktop_browser:
        from tools.browser_session import run_fenced
        dispatched = run_fenced({"features": {"local": True}}, dispatch)
    else:
        dispatched = dispatch()
    if not managed:
        _reload_if_republished(env, generation)
        _own_harness_daemon(env)
    if "proc" not in dispatched:
        if "error_result" in dispatched:
            return dispatched["error_result"]
        return tool_result(dispatched)
    proc = dispatched["proc"]

    # browser_vault fill registers injected values with this forced model-egress
    # boundary. Preserve raw stdout only for screenshot-path detection below.
    result = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": redact_sensitive_text(proc.stdout, force=True),
    }
    if workspace:
        result["workspace"] = workspace
    if session:
        result["session"] = session
    result.update(_take_real_profile_note(route_key))
    stderr = redact_sensitive_text((proc.stderr or "").strip(), force=True)
    if len(stderr) > _STDERR_CAP_CHARS:
        stderr = stderr[:_STDERR_CAP_CHARS] + "\n… (stderr truncated)"
    if stderr:
        result["stderr"] = stderr
    screenshot = _find_screenshot(proc.stdout, started)
    if screenshot:
        result["screenshot_path"] = screenshot
        native = _native_screenshot_result(result, screenshot)
        if native is not None:
            return native
    return tool_result(result)


_HEADER_BASE = (
    "Drive a real browser for JS pages, logins, forms and clicks (Browser Use CLI). `code` runs as full "
    "Python (stdlib available); print(...) any data you need back. Start `code` with a one-line "
    "comment describing the step for the user in plain language, max 60 chars "
    "(e.g. `# Searching Amazon for paper towels`) — the UI shows it as the step label.\n\n"
    "STATE: the browser session and workspace persist across calls; Python variables do NOT (fresh "
    "interpreter each call). The workspace dir is $BH_AGENT_WORKSPACE (also `workspace` in every result); "
    "functions defined in agent_helpers.py there are auto-imported into every call. For multi-item tasks "
    "('all N products / every entry'), append each batch to a JSON/CSV file in the workspace, then read it "
    "back and aggregate in code — dedupe/count/sort with Python, not in your head — and verify the "
    "collected count against what was asked before answering.\n\n"
    "Batch each sub-procedure (navigate, wait, extract, act) into one call, but for long extractions "
    "prefer several medium calls that append to workspace files over "
    "one giant call, so progress survives timeouts."
)

_HEADER_VISION = (
    " Screenshots are attached to your context automatically: when the exec output contains a "
    "capture_screenshot() path, the image arrives with this tool's result and you inspect it directly "
    "with your own vision — never send browser screenshots to a separate vision tool."
)

_TEXT_FIRST = (
    "page_info() for state, js() for reading/extracting DOM text, fill_input(selector, text) for inputs, and "
    "js(\"document.querySelector('…').click()\") for clicks."
)

# The non-native header states a transport fact (screenshots do not ride back inside THIS
# tool's results: routing is not ``native`` — e.g. an explicit ``auxiliary.vision`` pin — or
# the route rejects tool-result media), never a verdict on the model's own vision: models
# quote "your model cannot view images" against themselves and skip user images (#131568).
# model_tools.get_tool_definitions() appends the vision_analyze pointer after
# SCREENSHOTS_DETACHED when the session actually has that tool.
SCREENSHOTS_DETACHED = " Screenshots are not attached to this tool's results."

_HEADER_TEXT_ONLY = SCREENSHOTS_DETACHED + " Work text-first: " + _TEXT_FIRST

# Lightpanda: no graphical renderer, and one CDP connection holds one page — a second
# Target.createTarget fails with TargetAlreadyLoaded (drop the new_tab() sentence once
# lightpanda-io/browser#1962 lands).
_HEADER_LIGHTPANDA = (
    " The local engine is Lightpanda (no graphical renderer, one page per session): capture_screenshot() "
    "is unavailable, so work text-first: " + _TEXT_FIRST + " Navigate with new_tab(url) exactly once, then "
    "goto_url(url) for every later navigation — a second new_tab() fails with TargetAlreadyLoaded."
)

# Pinned quick-reference for the CLI's pre-imported helpers, replacing the live
# ``browser-use skill`` fetch (uncontrolled third-party text in every schema: version
# drift, supply-chain exposure, byte-unstable prompt). A/B benchmarked ~equal.
_HELPERS_DIGEST = (
    "\n\nHELPERS (pre-imported): new_tab(url) opens/navigates (use for the FIRST navigation), goto_url(url) "
    "navigates the current tab, wait_for_load() after navigation, page_info() summarizes the current page "
    "state, js(expr) evaluates a JS expression and returns its value (js('document.title'); wrap function "
    "bodies as js('(() => {...})()') — a bare '() => {...}' returns the function itself, uncalled), "
    "fill_input(selector, text) types into inputs, click_at_xy(x, y) clicks viewport coordinates, "
    "capture_screenshot() saves and prints a screenshot path, cdp('Domain.method', **kwargs) is raw CDP — "
    "cdp('Accessibility.getFullAXTree')['nodes'] lists every element's role/name/backendDOMNodeId (filter "
    "in Python before printing; it is thousands of nodes), then cdp('DOM.getBoxModel', backendNodeId=n) "
    "gives click coordinates. ensure_real_tab() recovers from a stale/internal tab. Close tabs you opened with "
    "close_tab(target_id) when you are done with them; Hermes closes a task's tabs when it ends and caps each "
    "task at 3 open tabs. Login walls: never guess "
    "credentials; see the vault note below if present, otherwise stop and ask the user."
)


def _description_header() -> str:
    """Header tailored to whether screenshots ride back natively in this tool's results."""
    if _lazy_call("tools.browser_lightpanda", "lightpanda_engine_status", (False, ""),
                  "lightpanda engine status unavailable")[0]:  # no screenshots, whatever the model sees
        return _HEADER_BASE + _HEADER_LIGHTPANDA
    if _lazy_call("tools.vision_tools", "_should_use_native_vision_fast_path", False, ""):
        return _HEADER_BASE + _HEADER_VISION
    return _HEADER_BASE + _HEADER_TEXT_ONLY


def _dynamic_schema_overrides() -> dict:
    overrides: dict = {"description": _description_header() + _HELPERS_DIGEST}
    # ``local`` exists ONLY when the user consented to real-profile browsing — everyone
    # else's schema carries zero extra surface. The caller memoizes on config.yaml mtime,
    # so toggling consent applies next session, not mid-chat.
    if _real_profile_consented():
        props = dict(BROWSER_EXEC_SCHEMA["parameters"]["properties"])
        props["local"] = {
            "type": "boolean", "default": False,
            "description": ("Drive the user's own local browser (a Hermes-managed copy of their real "
                            "default-Chromium profile, logins/cookies included) instead of the configured "
                            "cloud browser backend. Use when the user asks to act as themselves — their "
                            "accounts, their sessions. No-op when the backend is already local. Default false."),
        }
        overrides["parameters"] = {**BROWSER_EXEC_SCHEMA["parameters"], "properties": props}
    return overrides


BROWSER_EXEC_SCHEMA = {
    "name": "browser_exec",
    # Static fallback description, used only when the managed CLI is unavailable
    "description": (_HEADER_BASE + _HELPERS_DIGEST
                    + "\n\n(The browser-use CLI is not installed yet. Install it with `hermes tools` (Browser Automation → Browser Use).)"),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python using the pre-imported browser helpers."},
            "session": {"type": "string", "description": "Omit for normal work: the shared default session is reused across sites, tabs and calls. Set a name only when this task runs at the same time as another browser task and needs its own tab (or, on cloud backends, its own browser); each name starts another daemon and browser connection, stopped when its agent exits. Reuse the same name on every related call; never invent per-site or per-job names."},
            "timeout_s": {"type": "integer", "default": _DEFAULT_TIMEOUT_S,
                          "description": f"Max seconds to wait for the code to finish (max {_MAX_TIMEOUT_S})."},
        },
        "required": ["code"],
    },
}


# browser_exec is additionally gated at tool-definition time — sessions whose toolsets
# lack ``terminal`` never see it (model_tools._compute_tool_definitions). check_fn only
# answers "is browser-harness importable"; surface policy lives with the session.
from tools.registry import registry


# The LOCAL browser this context's browser_exec call ran on ("local" Chromium or "lightpanda", from its session),
# else None: a configured cloud provider did not serve a hybrid ::local sidecar, cloud-fallback or real-profile
# call, and browser.engine read afterwards may name another engine than the session's, so neither labels it.
_exec_local_engine: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("browser_exec_local_engine",
                                                                                   default=None)


def _handle_browser_exec(args: dict, **kw):
    """Registry handler; each call counts against the backend that served it (shared metrics, opt-in)."""
    from hermes_cli.observability.shared_metrics_loop import record_browser_call
    from tools.browser_session_cloud import browser_backend_name
    token = _exec_local_engine.set(None)
    try:
        return record_browser_call(lambda: browser_exec(
            code=args.get("code", ""), session=args.get("session", "") or "",
            timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S), task_id=kw.get("task_id"),
            local=bool(args.get("local", False)),
        ), lambda: browser_backend_name(local_engine=_exec_local_engine.get()))
    finally:
        _exec_local_engine.reset(token)


registry.register(
    name="browser_exec",
    toolset="browser-use",
    schema=BROWSER_EXEC_SCHEMA,
    handler=_handle_browser_exec,
    check_fn=browser_exec_available,
    dynamic_schema_overrides=_dynamic_schema_overrides,
    emoji="🌐",
)
