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
    if path.is_symlink():
        raise ValueError("runtime descriptor must not be a symlink")
    with path.open("rb") as stream:
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


def disarm(config, *, home):
    """Retain the descriptor/evidence; revoke new boots and monitored consumers."""
    raw = dict(config.get("kanban", {}).get("fairness_capture", {}))
    raw["enabled"] = True  # permit rollback after the config was disabled
    settings = resolve({"kanban": {"fairness_capture": raw}}, home=Path(home))
    root = settings.directory
    with budget._locked(root):
        if (root / NAME).exists():
            os.replace(root / NAME, root / DISARMED)


def _monitor(session, path, expected):
    while not session.stopping.wait(0.5):
        try:
            unchanged = _read(path) == expected
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
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
        from agent.memory_provider import spawn_context_thread

        session.activation_monitor = spawn_context_thread(
            lambda: _monitor(session, settings.directory / NAME, raw),
            name="fairness-arm-monitor", daemon=True, passive=True)
        session.activation_monitor.start()
        return session


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
