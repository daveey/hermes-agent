"""Durable copy of the session-list kanban annotation, so a backend restart does not start cold.

``kanban_session_activity._annotation_snapshot`` serves the last good annotation while one refresh runs, but that
copy lived only in process memory. Every restart (the core updater restarts the always-on dashboard several times
an hour) began cold, and a cold annotation makes every session poll wait on the one in-flight read. The Desktop
reconnects about 3 s after the backend starts, so the boot prewarm and the reconnect burst run together under the
GIL: on hermes-hub the ~1.2 s read took 15-17 s and up to 11 ``/api/profiles/sessions`` workers sat in
``_annotation_snapshot`` for 5-6 s (t_01a18b5c8b3db19c). The last published annotation is now written here and a
cold poll serves it (when recent) while the first read of the new process runs. [daveey]

The copy only seeds a cold start: it is never served once this process has its own read, it is ignored past
``MAX_AGE_S`` or for another scope, and the caller stamps rows with the copy's own time, never "now".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# A restart gap is seconds; a copy older than this describes a backend that has been down, not one restarting.
MAX_AGE_S = 300.0
_PARTS = 11
# [daveey] 2: lists and dicts are wrapped as ``{"l"}``/``{"d"}`` (PR #2200). A version 1 copy is ignored (one cold
# start), and an older core ignores a version 2 copy instead of seeding the wrappers as data.
_VERSION = 2


# Off under the test harness, where many tests share one scope and a copy would leak between them; the copy's own
# tests set ``FORCE``.
FORCE = False
# The copy this process loaded per scope, served until its own read publishes: ``(parts, saved_at)``.
_seeded: dict[tuple[str, ...], tuple[tuple, float]] = {}
# The last ``(parts, time)`` written per scope, so an unchanged read is not rewritten on every poll.
_saved: dict[tuple[str, ...], tuple[tuple, float]] = {}


def enabled() -> bool:
    return FORCE or not os.environ.get("HERMES_TEST_ISOLATION")


def reset() -> None:
    _seeded.clear()
    _saved.clear()


def published(scope: tuple[str, ...], parts: tuple) -> None:
    """This process read ``parts`` itself: retire any loaded copy and persist a changed (or half-aged) read."""
    _seeded.pop(scope, None)
    saved = _saved.get(scope)
    now = time.time()
    if saved is not None and saved[0] == parts and now - saved[1] < MAX_AGE_S / 2:
        return
    if save(scope, parts):
        _saved[scope] = (parts, now)


def seed(scope: tuple[str, ...], last: dict[tuple[str, ...], tuple], freeze: Callable[[tuple], tuple]) -> tuple | None:
    """Publish the previous process's recent copy into ``last`` (unless a read beat it there) and return what
    ``last`` now holds for ``scope``; None when there is no usable copy."""
    loaded = load(scope)
    if loaded is None:
        return None
    parts = freeze(loaded[0])
    current = last.setdefault(scope, parts)
    if current is parts:
        _seeded[scope] = (parts, loaded[1])
    return current


def observed_at(parts: Any) -> float | None:
    """The saved time of ``parts`` when it is a loaded copy rather than this process's own read."""
    for seeded, saved_at in list(_seeded.values()):
        if seeded is parts:
            return saved_at
    return None


def _path(scope: tuple[str, ...]) -> Path:
    from hermes_constants import get_hermes_home

    digest = hashlib.sha256(json.dumps(list(scope)).encode("utf-8")).hexdigest()[:16]
    return Path(get_hermes_home()) / "cache" / "kanban-annotation" / f"{digest}.json"


def _encode(value: Any) -> Any:
    # [daveey] Lists, tuples and nested dicts keep their own type, so a decoded copy equals the read exactly (the
    # execution part's evidence dicts hold lists).
    if isinstance(value, (set, frozenset, tuple, list)):
        items = [_encode(v) for v in value]
        if isinstance(value, (set, frozenset)):
            items.sort(key=lambda v: json.dumps(v, sort_keys=True))
            return {"s": items}
        return {"l": items} if isinstance(value, list) else {"t": items}
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("annotation copy: non-string dict key")
        return {"d": {k: _encode(v) for k, v in value.items()}}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"s"}:
            return frozenset(_decode(v) for v in value["s"])
        if set(value) == {"t"}:
            return tuple(_decode(v) for v in value["t"])
        if set(value) == {"l"}:
            return [_decode(v) for v in value["l"]]
        if set(value) == {"d"} and isinstance(value["d"], dict):
            return {k: _decode(v) for k, v in value["d"].items()}
        return {k: _decode(v) for k, v in value.items()}
    return value


def save(scope: tuple[str, ...], parts: tuple) -> bool:
    """Write ``parts`` for ``scope`` atomically. Never raises: the copy only speeds a cold start."""
    if not enabled():
        return False
    try:
        if len(parts) not in (_PARTS, _PARTS + 1):
            return False
        body = {
            "version": _VERSION,
            "scope": list(scope),
            "saved_at": time.time(),
            "parts": [{str(k): _encode(v) for k, v in part.items()} for part in parts],
        }
        path = _path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # health: allow BLE001 -- best-effort cache write; the in-memory annotation is unaffected
        log.debug("kanban annotation copy not saved", exc_info=True)
        return False


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _decode_parts(raw: Any) -> tuple | None:
    if not isinstance(raw, list) or len(raw) not in (_PARTS, _PARTS + 1) or not all(isinstance(p, dict) for p in raw):
        return None
    return tuple({k: _decode(v) for k, v in part.items()} for part in raw)


def save_host(scope: tuple[str, ...], fingerprint: str, read_at: float, parts: tuple) -> bool:
    """[daveey] The host copy other processes adopt (``kanban_annotation_host``): ``parts`` answered exactly the
    inputs ``fingerprint`` names, read at ``read_at``. Never raises."""
    if not enabled() or len(parts) not in (_PARTS, _PARTS + 1):
        return False
    try:
        body = {"version": _VERSION, "scope": list(scope), "fingerprint": fingerprint, "read_at": float(read_at),
                "parts": [{str(k): _encode(v) for k, v in part.items()} for part in parts]}
        _write_json(_path(scope).with_suffix(".host.json"), body)
        return True
    except Exception:  # health: allow BLE001 -- best-effort; other processes then read for themselves
        log.debug("kanban annotation host copy not saved", exc_info=True)
        return False


def load_host(scope: tuple[str, ...]) -> tuple[str, float, tuple] | None:
    """``(fingerprint, read_at, parts)`` of the host copy for ``scope``, else None. Never raises."""
    if not enabled():
        return None
    try:
        body = json.loads(_path(scope).with_suffix(".host.json").read_text(encoding="utf-8-sig"))
        if body.get("version") != _VERSION or body.get("scope") != list(scope):
            return None
        fingerprint, read_at = body["fingerprint"], float(body["read_at"])
        parts = _decode_parts(body["parts"])
        if not isinstance(fingerprint, str) or parts is None or not read_at <= time.time():
            return None
        return fingerprint, read_at, parts
    except FileNotFoundError:
        return None
    except Exception:  # health: allow BLE001 -- a bad copy is no copy
        log.debug("kanban annotation host copy unreadable", exc_info=True)
        return None


def touch_demand(scope: tuple[str, ...]) -> None:
    """Mark ``scope`` as polled now (the host refresher keeps such scopes' copies current). Never raises."""
    if not enabled():
        return
    try:
        path = _path(scope).with_suffix(".demand")
        try:
            os.utime(path)
        except FileNotFoundError:
            _write_json(path, {"scope": list(scope)})
    except Exception:  # health: allow BLE001 -- demand only steers the refresher
        log.debug("kanban annotation demand not recorded", exc_info=True)


def demand_age(scope: tuple[str, ...]) -> float | None:
    """Seconds since any process last polled ``scope`` (None: never, or unreadable)."""
    try:
        return time.time() - _path(scope).with_suffix(".demand").stat().st_mtime
    except OSError:
        return None


def load(scope: tuple[str, ...], *, max_age_s: float = MAX_AGE_S) -> tuple[tuple, float] | None:
    """``(parts, saved_at)`` for ``scope`` when a copy at most ``max_age_s`` old exists, else None. Never raises."""
    if not enabled():
        return None
    try:
        body = json.loads(_path(scope).read_text(encoding="utf-8-sig"))
        if body.get("version") != _VERSION or body.get("scope") != list(scope):
            return None
        saved_at = float(body["saved_at"])
        if not 0 <= time.time() - saved_at <= max_age_s:
            return None
        raw = body["parts"]
        if not isinstance(raw, list) or len(raw) not in (_PARTS, _PARTS + 1) or not all(isinstance(p, dict) for p in raw):
            return None
        parts = tuple({k: _decode(v) for k, v in part.items()} for part in raw)
        return parts, saved_at
    except FileNotFoundError:
        return None
    except Exception:  # health: allow BLE001 -- a bad copy is no copy; the poll waits for the real read
        log.debug("kanban annotation copy unreadable", exc_info=True)
        return None
