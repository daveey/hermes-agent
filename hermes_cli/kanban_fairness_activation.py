"""Explicit operator descriptor and startup-only runtime binding. [daveey]

Config alone is inert. No reader/scorer/config-loader calls this module. A small
monitor observes disarm outside scheduling locks; existing processes close within
one poll plus the shared runtime shutdown allowance. No journal is auto-armed.
"""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading

from hermes_cli.kanban_fairness_config import resolve
from hermes_cli import kanban_fairness_budget as budget
from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli.kanban_fairness_schema import make_record

NAME = "runtime-arm.json"
DISARMED = "runtime-disarmed.json"
MAX_DESCRIPTOR = 8192
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)


def _validate(value, settings, home):
    keys = {"version", "capture_id", "directory", "max_bytes", "home", "board",
            "source_gateway", "board_uid", "clock_error_ns"}
    if (type(value) is not dict or set(value) != keys or type(value["version"]) is not int
            or value["version"] != 1 or value["capture_id"] != settings.capture_id
            or value["directory"] != str(settings.directory) or value["home"] != str(home)
            or type(value["max_bytes"]) is not int or value["max_bytes"] != settings.max_bytes
            or not isinstance(value["board"], str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value["board"]) is None):
        raise ValueError("invalid runtime arm scope")
    # Reuse the frozen envelope identity rules, without persisting a synthetic event.
    make_record(capture_id=settings.capture_id, source_gateway=value["source_gateway"],
                board_uid=value["board_uid"], source_epoch="validation", seq=1, kind="loss",
                wall_ns=0, mono_ns=None, clock_error_ns=value["clock_error_ns"],
                payload=runtime.Session.loss_payload("clock_gap", 1))
    return value


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DESCRIPTOR:
            raise ValueError("runtime descriptor must be a bounded regular file")
        raw = stream.read(MAX_DESCRIPTOR + 1)
    if len(raw) > MAX_DESCRIPTOR:
        raise ValueError("runtime descriptor exceeds bound")
    return raw


def arm(config, *, home, board, source_gateway, board_uid, clock_error_ns=None):
    """Explicit operator action; never invoked by startup/config loading."""
    home = Path(home)
    settings = resolve(config, home=home)
    if settings is None:
        raise ValueError("runtime capture config is disabled")
    value = _validate(dict(version=1, capture_id=settings.capture_id,
                          directory=str(settings.directory), max_bytes=settings.max_bytes,
                          home=str(home), board=board, source_gateway=source_gateway,
                          board_uid=board_uid, clock_error_ns=clock_error_ns), settings, home)
    raw = json.dumps(value, sort_keys=True).encode("utf-8")
    if len(raw) > MAX_DESCRIPTOR:
        raise ValueError("runtime descriptor exceeds bound")
    root = settings.directory
    budget.initialize(root, settings.max_bytes)
    with budget._locked(root):
        if any((root / name).exists() for name in (NAME, DISARMED, "runtime")):
            raise ValueError("runtime capture cannot be rearmed or replaced; use a fresh capture")
        if budget._used(root) + len(raw) > budget._limit(root):
            raise budget.BudgetExceeded("capture cannot hold runtime descriptor")
        budget._write(root / NAME, raw)
    return value


def disarm(config, *, home, directory=None):
    """Revoke an existing descriptor, even after config reset; never create one."""
    home = Path(home)
    if directory is None:
        raw = dict(config.get("kanban", {}).get("fairness_capture", {}))
        raw["enabled"] = True  # permit rollback after the config was disabled
        settings = resolve({"kanban": {"fairness_capture": raw}}, home=home)
        root = settings.directory
    else:
        root = Path(directory)
        if not root.is_absolute():
            raise ValueError("disarm directory must be absolute")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("disarm requires an existing capture directory")
    with budget._locked(root):
        path = root / NAME if (root / NAME).exists() else root / DISARMED
        value = json.loads(_read(path))
        if type(value) is not dict:
            raise ValueError("invalid runtime arm descriptor")
        options = {"enabled": True, "capture_id": value.get("capture_id"),
                   "directory": str(root), "max_bytes": value.get("max_bytes")}
        settings = resolve({"kanban": {"fairness_capture": options}}, home=home)
        _validate(value, settings, home)
        if path.name == NAME:
            os.replace(path, root / DISARMED)


def _authority(path, expected):
    """Only an exact retained descriptor proves intentional operator revocation."""
    try:
        return "armed" if _read(path) == expected else "lost"
    except FileNotFoundError:
        try:
            return "disarmed" if _read(path.parent / DISARMED) == expected else "lost"
        except (OSError, ValueError):
            return "lost"
    except (OSError, ValueError):
        return "lost"


def _monitor(session, path, expected):
    while not session.stopping.wait(0.5):
        state = _authority(path, expected)
        if state != "armed":
            if state == "lost":
                session.abandoned.set()
            session.close()
            return


def start(config, *, home):
    """Startup-only, idempotent binding. Disabled path performs no capture IO."""
    home = Path(home)
    settings = resolve(config, home=home)
    if settings is None:
        return None
    with _LOCK:
        try:
            raw = _read(settings.directory / NAME)
        except FileNotFoundError:
            return None
        value = _validate(json.loads(raw), settings, home)
        # Never repair missing/invalid budget on an armed production capture.
        if budget._limit(settings.directory) != settings.max_bytes:
            raise ValueError("runtime arm budget mismatch")
        key = runtime._key(value["board"], home)
        if key in runtime._SESSIONS:
            return runtime._SESSIONS[key]
        session = runtime.activate(config, home=home, board=value["board"],
                                   source_gateway=value["source_gateway"], board_uid=value["board_uid"],
                                   clock_error_ns=value["clock_error_ns"])
        path = settings.directory / NAME
        session.authority_valid = lambda: _authority(path, raw) != "lost"
        state = _authority(path, raw)
        if state != "armed":
            if state == "lost":
                session.abandoned.set()
            session.close()
            return None
        from agent.memory_provider import spawn_context_thread

        session.activation_monitor = spawn_context_thread(
            lambda: _monitor(session, settings.directory / NAME, raw),
            name="fairness-arm-monitor", daemon=True, passive=True)
        session.activation_monitor.start()
        return session


def hydrate_worker_config(config, live):
    """Owner admission refreshes observations only, never frozen scheduler policy."""
    from copy import deepcopy

    block = live.get("kanban", {})
    prior = config.get("kanban", {})
    if (isinstance(prior, dict) and "fairness_capture" not in prior
            and (not isinstance(block, dict) or "fairness_capture" not in block)):
        return config
    result = deepcopy(config)
    capture = block.get("fairness_capture", {}) if isinstance(block, dict) else {}
    if not isinstance(result.get("kanban"), dict):
        result["kanban"] = {}
    result["kanban"]["fairness_capture"] = deepcopy(capture)
    return result


def boot(config=None, *, home=None):
    """Production startup: evidence failure cannot refuse normal queue service."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        if home is None:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
        return start(config, home=home)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        _LOG.warning("fairness runtime startup incomplete (%s)", type(exc).__name__)
        return None


@contextmanager
def serving(config=None, *, home=None):
    session = boot(config, home=home)
    try:
        yield session
    finally:
        if session is not None:
            session.close()
