"""Why the Desktop painted a chat red: the stripe reasons the Desktop reports, kept where the agent can read them.

[daveey] The sidebar's red ``stuck`` stripe is derived in the Desktop renderer from inputs only it holds (a pending
prompt on some runtime, a paused owner queue, a quiet turn) plus row fields the backend stamps (blocked cards, a
review wait, a ``decision`` outcome). An agent asked "why are you red" could see none of the renderer half and
guessed (selfheal desktop-54ae373894: it blamed a scheduled card while the real cause was a dead runtime's clarify).

The Desktop now POSTs every change of a row's red verdict (into stuck, reasons changed, out of stuck) to the
session's own gateway. This module appends it to ``<profile home>/logs/desktop-stripe.jsonl`` (size-capped, one
rotation) and logs one INFO line; ``hermes sessions outcome <id>`` prints the latest report next to the outcome.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "desktop-stripe.jsonl"
MAX_BYTES = 2 * 1024 * 1024
MAX_REASONS = 16
MAX_REASON_CHARS = 200
_TAIL_BYTES = 256 * 1024
_STRIPES = frozenset({"attention", "blocked", "done", "periodic", "stalled", "stuck", "waiting", "none"})
_lock = threading.Lock()


def report_path(home: Path) -> Path:
    return Path(home) / "logs" / FILE_NAME


def _clean_reasons(reasons: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for reason in reasons or ():
        text = " ".join(str(reason).split())[:MAX_REASON_CHARS]
        if text:
            out.append(text)
        if len(out) >= MAX_REASONS:
            break
    return out


def invariant_violated(stripe: Optional[str], reasons: Iterable[str]) -> bool:
    """True when the Desktop reported a stripe that is neither red (``stuck``) nor a ⌘B park (``blocked``) together
    with a ``needs_you:<n>`` reason: an open human ask that did not paint red (``sessionStatusStripe``'s invariant)."""
    return stripe not in ("stuck", "blocked") and any(str(r).startswith("needs_you:") for r in reasons or ())


def record(home: Path, session_id: str, stripe: Optional[str], reasons: Iterable[Any], *,
           previous: Optional[str] = None, now: Optional[float] = None) -> dict[str, Any]:
    """Append one report and log it. ``stripe`` None means the row has no stripe (``none``)."""
    entry = {
        "at": round(now if now is not None else time.time(), 3),
        "session_id": str(session_id),
        "stripe": stripe if stripe in _STRIPES else ("none" if not stripe else "unknown"),
        "previous": previous if previous in _STRIPES else ("none" if not previous else "unknown"),
        "reasons": _clean_reasons(reasons),
    }
    logger.info("desktop stripe report: session=%s %s->%s reasons=%s", entry["session_id"], entry["previous"],
                entry["stripe"], ",".join(entry["reasons"]) or "-")
    if invariant_violated(entry["stripe"], entry["reasons"]):
        logger.warning("stripe-invariant-violation %s stripe=%s reasons=%s", entry["session_id"], entry["stripe"],
                       ",".join(entry["reasons"]))
    path = report_path(home)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size + len(line) > MAX_BYTES:
                os.replace(path, path.with_name(FILE_NAME + ".1"))
        except FileNotFoundError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return entry


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read()
    except FileNotFoundError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > _TAIL_BYTES else lines


def latest(home: Path, session_ids: Iterable[str]) -> Optional[dict[str, Any]]:
    """The newest report for any of ``session_ids`` (a lineage), or None."""
    wanted = {str(s) for s in session_ids if s}
    if not wanted:
        return None
    path = report_path(home)
    for candidate in (path, path.with_name(FILE_NAME + ".1")):
        for line in reversed(_tail_lines(candidate)):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("session_id") in wanted:
                return entry
    return None


def describe(entry: Optional[dict[str, Any]], *, now: Optional[float] = None) -> str:
    if not entry:
        return "no Desktop stripe report"
    age = max(0.0, (now if now is not None else time.time()) - float(entry.get("at") or 0))
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(entry.get("at") or 0)))
    reasons = ", ".join(entry.get("reasons") or ()) or "-"
    # `at` is when the latest report arrived (a red edge, a cause change or a first sighting after a restart),
    # not when the stripe turned that colour.
    return f"Desktop stripe {entry.get('stripe')} as of {when} ({age / 60:.0f} min ago): {reasons}"
