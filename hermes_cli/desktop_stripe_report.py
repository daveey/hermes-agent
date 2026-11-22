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
           previous: Optional[str] = None, now: Optional[float] = None,
           observation: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Append one report and log it. ``stripe`` None means the row has no stripe (``none``)."""
    entry = {
        "at": round(now if now is not None else time.time(), 3),
        "session_id": str(session_id),
        "stripe": stripe if stripe in _STRIPES else ("none" if not stripe else "unknown"),
        "previous": previous if previous in _STRIPES else ("none" if not previous else "unknown"),
        "reasons": _clean_reasons(reasons),
    }
    if observation is not None:
        for key in ("status_version", "status", "identity", "revision", "generation", "coverage", "surface",
                    "reason_codes", "mounted", "observation_client", "observation_lifetime", "observation_order",
                    "local_override"):
            entry[key] = observation.get(key)
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
            # A committed-paint observation (it names its surface) is not a reasons report.
            if isinstance(entry, dict) and entry.get("session_id") in wanted and not entry.get("surface"):
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


SURFACES = ("row", "tab", "dot")


def compare(canonical: Optional[dict], observation: Optional[dict]) -> dict[str, Any]:
    """Compare actual paint only at the exact qualified owner, revision and generation."""
    def verdict(state, reason, drift=None):
        return {"state": state, "reason": reason, "drift": drift}

    if not canonical or not observation:
        return verdict("unknown", "missing_observation_or_status")
    if (type(canonical.get("status_version")) is not int or canonical.get("status_version") != 1
            or type(observation.get("status_version")) is not int or observation.get("status_version") != 1):
        return verdict("unknown", "unsupported_version")
    if observation.get("mounted") is not True or not _lifecycle_key(observation):
        return verdict("unknown", "not_mounted")
    if "at" in observation and time.time() - float(observation.get("at") or 0) > 300:
        return verdict("unknown", "expired_observation")
    identity = canonical.get("identity")
    if not isinstance(identity, dict) or any(not isinstance(identity.get(k), str) or not identity[k] for k in
            ("gateway", "profile", "session_id", "owner_epoch")):
        return verdict("unknown", "missing_identity")
    if observation.get("identity") != identity:
        return verdict("refused", "wrong_owner")
    if observation.get("status") != "canonical" or observation.get("surface") not in SURFACES:
        return verdict("unknown", "not_mounted_canonical")
    if observation.get("local_override"):
        # The surface painted its own live turn or an unconfirmed park over this revision: by design not this
        # revision's paint, so it is neither agreement nor drift.
        return verdict("unknown", "local_override")
    revision, expected = observation.get("revision"), canonical.get("revision")
    if type(revision) is not int or type(expected) is not int or revision < 0 or expected < 0:
        return verdict("unknown", "missing_revision")
    if revision != expected:
        return verdict("stale" if revision < expected else "unknown", "revision_mismatch")
    if (type(canonical.get("generation")) is not int or canonical["generation"] < 0
            or type(observation.get("generation")) is not int
            or observation.get("generation") != canonical["generation"]):
        return verdict("unknown", "generation_mismatch")
    if (canonical.get("coverage", {}).get("state") not in {"complete", "unknown"}
            or observation.get("coverage") != canonical.get("coverage")):
        return verdict("unknown", "coverage_mismatch")
    expected_stripe = canonical.get("presentation", {}).get("stripe")
    actual = observation.get("stripe")
    if expected_stripe not in _STRIPES or actual not in _STRIPES:
        return verdict("unknown", "missing_paint")
    drift = expected_stripe != actual
    return verdict("mismatch" if drift else "agreement", "paint_mismatch" if drift else "exact_paint", drift)


MAX_OBSERVATION_INSTANCES = 512


def _lifecycle_key(observation: dict) -> Optional[tuple[str, int]]:
    client = observation.get("observation_client")
    lifetime, order = observation.get("observation_lifetime"), observation.get("observation_order")
    if (not isinstance(client, str) or not 0 < len(client) <= 128
            or type(lifetime) is not int or lifetime < 0
            or type(order) is not int or order < 0):
        return None
    return client, lifetime


def surface_observations(home: Path, session_ids: Iterable[str], canonical: Optional[dict]) -> dict:
    """Independent client/mount custody; sequences are comparable only within one lifetime.

    Any current exact mismatch is evidence of drift. Agreement requires every remaining mounted
    instance to agree. Tombstones are terminal, not an instruction to hide other mounted instances.
    The bounded tail and instance budget are diagnostic windows, not durable state authority.
    """
    wanted, observations, overflow = set(session_ids), {}, set()
    path = report_path(home)
    for candidate in (path, path.with_name(FILE_NAME + ".1")):
        for line in reversed(_tail_lines(candidate)):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("session_id") not in wanted:
                continue
            surface = entry.get("surface")
            if surface not in SURFACES:
                continue
            lifecycle = _lifecycle_key(entry)
            key = (surface, entry["session_id"], lifecycle)
            prior = observations.get(key)
            if prior is None and len(observations) >= MAX_OBSERVATION_INSTANCES:
                overflow.add(surface)
                continue
            # An unmount wins forever for this lifetime, even against an invalid later mount.
            if prior is not None and prior.get("mounted") is False:
                continue
            if (prior is None or entry.get("mounted") is False
                    or (lifecycle and entry["observation_order"] > prior["observation_order"])):
                observations[key] = entry
    result = {}
    for surface in SURFACES:
        instances = [{**compare(canonical, obs), "observation": obs}
                     for key, obs in observations.items() if key[0] == surface]
        active = [item for item in instances if item["observation"].get("mounted") is not False]
        mismatch = next((item for item in active if item["state"] == "mismatch"), None)
        uncertain = next((item for item in active if item["state"] != "agreement"), None)
        selected = mismatch or uncertain or next(iter(active or instances), None)
        if surface in overflow:
            summary = {"state": "unknown", "reason": "observation_budget_exceeded", "drift": None, "observation": None}
        else:
            summary = selected or {**compare(canonical, None), "observation": None}
        result[surface] = {**summary, "instances": instances}
    return result
