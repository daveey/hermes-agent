"""Optional execution evidence v1, not admission, work outcomes or delivery authority.

See docs/execution-observations-v1.md. All timestamps here are source epoch
seconds; capacity event producers explicitly convert their *_ms fields.
"""
from __future__ import annotations

from copy import deepcopy
import math

VERSION = 1
STATES = frozenset({"running", "queued", "waiting", "needs_user", "done", "failed", "cancelled", "unknown"})
SOURCE_TTLS = {"sidebar": 180, "imported_owner": 600}
CAPACITY_CODES = frozenset({"memory_pressure", "provider_limit", "placement", "dependency", "update_drain"})


def epoch(value):
    """Finite nonnegative epoch seconds, rejecting bools and pathological integers."""
    if type(value) not in (int, float):
        return False
    return 0 <= value <= 8.64e12 and math.isfinite(value)


def capacity_reason(code, *, native_code, phase, evidence_ref):
    """Retain concurrent native diagnostics and their causal phase, never a work state."""
    if code not in CAPACITY_CODES or phase not in {"pre_claim", "post_claim", "post_start"}:
        raise ValueError("unknown capacity diagnostic or phase")
    if not all(isinstance(v, str) and v for v in (native_code, evidence_ref)):
        raise ValueError("native code and evidence reference required")
    return {"code": "capacity." + code, "native_code": native_code,
            "phase": phase, "evidence_ref": evidence_ref}


def _attempt_valid(value):
    return (type(value) is int and value > 0) or (isinstance(value, str) and bool(value))


def _identity_valid(identity):
    if not isinstance(identity, dict):
        return False
    required = ["gateway", "profile", "kind", "work_id"]
    if identity.get("kind") == "card":
        required.append("board")
    elif not any(isinstance(identity.get(k), str) and identity[k] for k in ("session", "lineage")):
        return False
    return (all(isinstance(identity.get(k), str) and identity[k] for k in required)
            and any(k in identity for k in ("run_id", "generation"))
            and all(_attempt_valid(identity[k]) for k in ("run_id", "generation") if k in identity))


def _unknown(out, reason, *, freshness="unknown"):
    out.update(state="unknown", freshness=freshness, unknown_reason=reason)
    return out


def adapt(value, *, now, source_class):
    """Copy a v1 observation, bounding its original validity; legacy input is unknown.

    Unknown fields (including human requests and historical delivery receipts) are
    retained verbatim. The adapter neither creates an observation timestamp nor
    infers progress from activity. A read never extends source validity.
    """
    raw = value if isinstance(value, dict) else {}
    out = deepcopy(raw)
    out.setdefault("observed_at", None)
    out.setdefault("valid_until", None)
    out.setdefault("last_activity", None)
    out.setdefault("last_progress", None)
    out["age_seconds"] = None
    out["last_known_state"] = (raw.get("state") if raw.get("state") in STATES - {"unknown"}
                               else raw.get("last_known_state"))
    if raw.get("version") != VERSION:
        return _unknown(out, "legacy_or_unsupported_version")
    if not _identity_valid(raw.get("identity")):
        return _unknown(out, "missing_qualified_identity")
    owner = raw.get("owner")
    if not isinstance(owner, dict) or not all(isinstance(owner.get(k), str) and owner[k]
                                             for k in ("gateway", "profile")):
        return _unknown(out, "missing_qualified_owner")
    refs = raw.get("evidence_refs")
    if not isinstance(refs, list) or not refs or not all(isinstance(v, str) and v for v in refs):
        return _unknown(out, "missing_evidence_references")
    if not all(isinstance(raw.get(k), str) and raw[k] for k in ("source", "source_revision")):
        return _unknown(out, "missing_source_identity")
    ttl = SOURCE_TTLS.get(source_class)
    observed, until = raw.get("observed_at"), raw.get("valid_until")
    if ttl is None or not all(epoch(v) for v in (now, observed, until)):
        return _unknown(out, "missing_or_invalid_source_timing")
    if until < observed or now < observed:
        return _unknown(out, "clock_inconsistent")
    until = min(until, observed + ttl)
    sources = raw.get("required_sources", [])
    if not isinstance(sources, list):
        return _unknown(out, "invalid_required_sources")
    for source in sources:
        if not isinstance(source, dict):
            return _unknown(out, "missing_required_source_timing")
        at, end = source.get("observed_at"), source.get("valid_until")
        if not all(epoch(v) for v in (at, end)):
            return _unknown(out, "missing_required_source_timing")
        if end < at or now < at:
            return _unknown(out, "clock_inconsistent")
        source_ttl = SOURCE_TTLS.get(source.get("source_class", source_class))
        if source_ttl is None:
            return _unknown(out, "invalid_required_source_class")
        until = min(until, end, at + source_ttl)
    out.update(valid_until=until, age_seconds=now - observed)
    if now >= until:
        return _unknown(out, "source_expired", freshness="stale")
    if raw.get("state") not in STATES:
        return _unknown(out, "unsupported_state")
    # A previous unknown read is not new producer evidence and cannot revive.
    if raw.get("state") == "unknown":
        return _unknown(out, raw.get("unknown_reason") or "source_unknown")
    out.update(freshness="current")
    out.pop("unknown_reason", None)
    return out
