"""[daveey] A background review a core update stopped survives the update's restart.

``review_idle_queue`` keeps a review that a core-update pause stopped (or that the update's admission hold refused)
in memory, so it runs once the hold lifts without a restart. The update itself restarts the backend, which loses
that queue. Each such review also leaves one small record here, keyed by session: the session's next finished
turn (``agent.turn_finalizer``) takes it and runs the review over its full conversation, even when that turn
would not have reviewed on its own. A review that does start clears the record. Best-effort, never raises:
reviews are self-improvement work, and a lost record only means one review fewer.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: A record older than this is stale (the session moved on long ago): it is dropped, not run.
MAX_AGE_S = 7 * 24 * 3600.0


def _path(session_id: str) -> Path:
    from hermes_constants import get_hermes_home

    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return Path(get_hermes_home()) / "cache" / "review-carry" / f"{digest}.json"


def _session(agent: Any) -> Optional[str]:
    session = getattr(agent, "session_id", None)
    return session if isinstance(session, str) and session else None


def record(agent: Any, kwargs: dict[str, Any]) -> bool:
    """Note that ``agent``'s session owes a review (its memory/skill flags). Returns whether it was written."""
    session = _session(agent)
    if session is None or kwargs.get("focus") is not None or kwargs.get("explicit"):
        return False  # /refine reviews are interactive: they are not carried across a restart
    tmp = None
    try:
        path = _path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune(path.parent)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps({
            "version": 1, "session_id": session, "created_at": time.time(),
            "review_memory": bool(kwargs.get("review_memory")), "review_skills": bool(kwargs.get("review_skills")),
        }), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        logger.warning("Could not record a carried background review (session=%s)", session[-12:], exc_info=True)
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return False


def _prune(directory: Path) -> None:
    """Drop records (and stray temp files) older than MAX_AGE_S: sessions that never took another turn."""
    cutoff = time.time() - MAX_AGE_S
    with contextlib.suppress(OSError):
        for entry in directory.iterdir():
            with contextlib.suppress(OSError):
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)


def clear(agent: Any) -> None:
    """The session's review started: its carried record (if any) is done."""
    session = _session(agent)
    if session is not None:
        try:
            _path(session).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not clear a carried background review", exc_info=True)


def take(agent: Any) -> Optional[dict[str, bool]]:
    """Remove and return the session's carried review flags (``review_memory``/``review_skills``), or None.
    A record for another session (hash collision), a stale record or an unreadable one is dropped."""
    session = _session(agent)
    if session is None:
        return None
    path = _path(session)
    # Claim by rename: of two processes taking the same record, exactly one gets it.
    claimed = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.taken")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("Could not claim a carried background review", exc_info=True)
        return None
    try:
        body = json.loads(claimed.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        body = None
    finally:
        with contextlib.suppress(OSError):
            claimed.unlink(missing_ok=True)
    if (not isinstance(body, dict) or body.get("session_id") != session
            or not isinstance(body.get("created_at"), (int, float))
            or not 0 <= time.time() - float(body["created_at"]) < MAX_AGE_S):
        return None
    flags = {"review_memory": body.get("review_memory") is True, "review_skills": body.get("review_skills") is True}
    return flags if any(flags.values()) else None
