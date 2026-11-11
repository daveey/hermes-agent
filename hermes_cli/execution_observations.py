"""Optional execution evidence v1, not admission, work outcomes or delivery authority.

See docs/execution-observations-v1.md. All timestamps here are source epoch
seconds; capacity event producers explicitly convert their *_ms fields.
"""
from __future__ import annotations

from copy import deepcopy
import math

VERSION = 1
MAX_SAFE_INTEGER = 9007199254740991
# Union of Python str.isspace and JavaScript String.trim sets, so blank/padding checks agree in both languages.
WHITESPACE = ("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
              "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
STATES = frozenset({"running", "queued", "waiting", "needs_user", "done", "failed", "cancelled", "unknown"})
SOURCE_TTLS = {"sidebar": 180, "imported_owner": 600}
CAPACITY_CODES = frozenset({"memory_pressure", "provider_limit", "placement", "dependency", "update_drain"})


def epoch(value):
    """Finite nonnegative epoch seconds, rejecting bools and pathological integers."""
    if type(value) not in (int, float):
        return False
    return 0 <= value <= 8.64e12 and math.isfinite(value)


def nonblank(value):
    return isinstance(value, str) and bool(value.strip(WHITESPACE))


def ident(value):
    """Identifier text: nonempty, not blank and without leading/trailing whitespace (no aliasing)."""
    return isinstance(value, str) and bool(value) and value == value.strip(WHITESPACE)


def _ttl(source_class):
    return SOURCE_TTLS.get(source_class) if isinstance(source_class, str) else None


def capacity_reason(code, *, native_code, phase, evidence_ref):
    """Retain concurrent native diagnostics and their causal phase, never a work state."""
    if code not in CAPACITY_CODES or phase not in {"pre_claim", "post_claim", "post_start"}:
        raise ValueError("unknown capacity diagnostic or phase")
    if not all(isinstance(v, str) and v for v in (native_code, evidence_ref)):
        raise ValueError("native code and evidence reference required")
    return {"code": "capacity." + code, "native_code": native_code,
            "phase": phase, "evidence_ref": evidence_ref}


def _integral(value):
    """Integer value of a JSON number in 1..2**53-1 (7.0 is the integer 7), else None."""
    if type(value) is float and value.is_integer():
        value = int(value)
    return value if type(value) is int and 1 <= value <= MAX_SAFE_INTEGER else None


def _attempt_valid(value):
    return _integral(value) is not None or ident(value)


def _identity_valid(identity):
    if not isinstance(identity, dict):
        return False
    required = ["gateway", "profile", "kind", "work_id"]
    if identity.get("kind") == "card":
        required.append("board")
    elif not any(ident(identity.get(k)) for k in ("session", "lineage")):
        return False
    if any(k in identity and not ident(identity[k]) for k in ("board", "session", "lineage")):
        return False
    return (all(ident(identity.get(k)) for k in required)
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
    state = raw.get("state")
    out["last_known_state"] = (state if isinstance(state, str) and state in STATES - {"unknown"}
                               else raw.get("last_known_state"))
    version = raw.get("version")
    if not (type(version) in (int, float) and version == VERSION):
        return _unknown(out, "legacy_or_unsupported_version")
    if not _identity_valid(raw.get("identity")):
        return _unknown(out, "missing_qualified_identity")
    owner = raw.get("owner")
    if not isinstance(owner, dict) or not all(ident(owner.get(k)) for k in ("gateway", "profile")):
        return _unknown(out, "missing_qualified_owner")
    refs = raw.get("evidence_refs")
    if not isinstance(refs, list) or not refs or not all(nonblank(v) for v in refs):
        return _unknown(out, "missing_evidence_references")
    if not all(nonblank(raw.get(k)) for k in ("source", "source_revision")):
        return _unknown(out, "missing_source_identity")
    ttl = _ttl(source_class)
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
        # Absent inherits the envelope class; explicit null/array/unknown is invalid.
        source_ttl = _ttl(source["source_class"]) if "source_class" in source else ttl
        if source_ttl is None:
            return _unknown(out, "invalid_required_source_class")
        until = min(until, end, at + source_ttl)
    out.update(valid_until=until, age_seconds=now - observed)
    if now >= until:
        return _unknown(out, "source_expired", freshness="stale")
    if not isinstance(state, str) or state not in STATES:
        return _unknown(out, "unsupported_state")
    # A previous unknown read is not new producer evidence and cannot revive.
    if state == "unknown":
        reason = raw.get("unknown_reason")
        return _unknown(out, reason if isinstance(reason, str) and reason else "source_unknown")
    out.update(freshness="current")
    out.pop("unknown_reason", None)
    return out




IDENTITY_FIELDS = ("gateway", "profile", "board", "session", "lineage", "kind", "work_id", "run_id", "generation")
PROGRESS_KINDS = frozenset({"artifact", "test_result", "work_result"})
NONHUMAN_WAIT_KINDS = frozenset({"dependency", "transient", "time"})
INTERACTIONS = frozenset({"approval", "reply", "user_terminal", "physical", "external"})
PROBE_FIELDS = ("resolved_check", "resolved_check_every")
WORK_KINDS = frozenset({"card", "turn", "process", "subagent"})


def _canonical(value):
    """Normalize a valid attempt so 7 and 7.0 are one identity; strings stay distinct from numbers."""
    number = _integral(value)
    return ("n", number) if number is not None else ("s", value)


def identity_key(identity):
    """An execution, not a PID or an unqualified task id. No attempt ordering inferred."""
    if not _identity_valid(identity):
        return None
    return tuple((k, _canonical(identity.get(k)) if k in ("run_id", "generation") and k in identity
                 else identity.get(k))
                 for k in IDENTITY_FIELDS)


def same_json(a, b):
    """JSON-semantic equality: bool is not a number; 7.0 equals 7; no coercion elsewhere."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b))
    if a is None or isinstance(a, str):
        return type(a) is type(b) and a == b
    return False


def _qualified(value):
    return isinstance(value, dict) and all(ident(value.get(k)) for k in ("gateway", "profile"))


def _wait_valid(wait, now):
    if not isinstance(wait, dict) or not _qualified(wait.get("owner")):
        return False
    target = wait.get("target")
    if not _qualified(target) or not all(ident(target.get(k)) for k in ("kind", "work_id")):
        return False
    if target["kind"] == "card" and not ident(target.get("board")):
        return False
    since, due = wait.get("since"), wait.get("due_at")
    return (nonblank(wait.get("kind")) and nonblank(wait.get("predicate"))
            and epoch(since) and since <= now and (due is None or (epoch(due) and due >= since)))


def _progress(out, now):
    at, evidence, observed = out.get("last_progress"), out.get("progress_evidence"), out.get("observed_at")
    kind, ref = out.get("last_progress_kind"), out.get("last_progress_ref")
    valid = (epoch(at) and epoch(now) and epoch(observed) and at <= observed and at <= now
             and isinstance(kind, str) and kind in PROGRESS_KINDS
             and isinstance(ref, str) and bool(ref)
             and isinstance(evidence, dict) and evidence.get("successful") is True
             and evidence.get("evidence_ref") == ref
             and epoch(evidence.get("observed_at")) and evidence["observed_at"] == at)
    if not valid:
        out.update(last_progress=None, last_progress_kind=None, last_progress_ref=None)


def _text_len(value):
    return len(value.strip(WHITESPACE))


def human_request_valid(value, *, wait_kind=None):
    """Visible request semantics shared with the TS projector. Probes are stripped before this runs."""
    if not isinstance(value, dict):
        return False
    if not all(nonblank(value.get(k)) for k in ("action", "why_human", "interaction", "scope", "evidence")):
        return False
    if value["interaction"] not in INTERACTIONS:
        return False
    if isinstance(wait_kind, str) and wait_kind in NONHUMAN_WAIT_KINDS:
        return False
    choices = value.get("choices")
    if choices is not None:
        if not isinstance(choices, list) or not 2 <= len(choices) <= 6:
            return False
        values = set()
        for choice in choices:
            if (not isinstance(choice, dict) or not nonblank(choice.get("label")) or _text_len(choice["label"]) > 60
                    or not nonblank(choice.get("value")) or _text_len(choice["value"]) > 120):
                return False
            values.add(choice["value"].strip(WHITESPACE))
        if len(values) != len(choices):
            return False
    return True


def _strip_probes(value):
    """Private probe commands never enter an observation: copy without them, original untouched."""
    raw = value if isinstance(value, dict) else {}
    request = raw.get("human_request")
    if not isinstance(request, dict) or not any(k in request for k in PROBE_FIELDS):
        return raw
    return {**raw, "human_request": {k: v for k, v in request.items() if k not in PROBE_FIELDS}}


def _wait_kind(out):
    wait = out.get("wait")
    return wait.get("kind") if isinstance(wait, dict) else None


def _delivered(out):
    receipt = out.get("delivery_receipt")
    key = identity_key(out.get("identity"))
    return (key is not None and isinstance(receipt, dict) and receipt.get("verified") is True
            and receipt.get("disposition") == "delivered"
            and nonblank(receipt.get("evidence_ref")) and identity_key(receipt.get("identity")) == key)


def _last_known(out):
    """Never claim done without a matching verified delivered receipt, whatever the source of the value."""
    known = out.get("last_known_state")
    valid = isinstance(known, str) and known in STATES - {"unknown"}
    out["last_known_state"] = known if valid and (known != "done" or _delivered(out)) else None


def _project(value, now, source_class, expected_identity):
    out = adapt(_strip_probes(value), now=now, source_class=source_class)
    _progress(out, now)
    if expected_identity is not None:
        expected = identity_key(expected_identity)
        if expected is None or expected != identity_key(out.get("identity")):
            return _unknown(out, "execution_identity_mismatch")
    if out["state"] == "unknown":
        return out
    if out["identity"]["kind"] not in WORK_KINDS:
        return _unknown(out, "unsupported_work_kind")
    if out["state"] == "needs_user" and not human_request_valid(out.get("human_request")):
        return _unknown(out, "missing_human_request")
    if out["state"] in {"waiting", "needs_user"} and not _wait_valid(out.get("wait"), now):
        return _unknown(out, "incomplete_wait_condition")
    if out["state"] == "needs_user" and _wait_kind(out) in NONHUMAN_WAIT_KINDS:
        return _unknown(out, "invalid_human_wait")
    if out["state"] == "done" and not _delivered(out):
        return _unknown(out, "unverified_completion")
    return out


def project(value, *, now, source_class="sidebar", expected_identity=None):
    """Strict work semantics on the published freshness envelope; no I/O or authority writes.

    Receipt adapters must supply existing verification, never infer it from a status
    label. Historical receipts/requests survive every failure, including expiration.
    Private ``resolved_check*`` probe fields are stripped from ``human_request`` on entry.
    """
    out = _project(value, now, source_class, expected_identity)
    _last_known(out)
    return out


def select(values, *, identity, now, source_class="sidebar"):
    """Select within an authoritative current execution, ignoring old-run arrivals.

    Revisions are opaque; conflicting records at the same source time cannot be
    ordered by arrival or lexicographic revision. Result never depends on input
    order. Different sources are composed by their producer with
    required_sources, not implicitly merged here. Malformed entries are ignored.
    """
    key = identity_key(identity)
    candidates = values if isinstance(values, (list, tuple)) else []
    matches = [v for v in candidates if isinstance(v, dict) and key is not None
               and identity_key(v.get("identity")) == key]
    base = {"identity": deepcopy(identity)}
    if not matches:
        return project(base, now=now, source_class=source_class)
    if any(not epoch(v.get("observed_at")) for v in matches):
        return _unknown(project(base, now=now, source_class=source_class), "invalid_candidate_timing")
    latest_at = max(v["observed_at"] for v in matches)
    latest = [v for v in matches if v["observed_at"] == latest_at]
    if (any(not same_json(v, latest[0]) for v in latest[1:])
            or any(not same_json(v.get("source"), matches[0].get("source")) for v in matches[1:])):
        return _unknown(project(base, now=now, source_class=source_class), "conflicting_source_evidence")
    return project(latest[0], now=now, source_class=source_class)


def summarize(values, *, now, source_class="sidebar"):
    """Concurrent state counts; a fresh worker never hides an unknown source.

    An empty list alone is not a fresh, complete inventory receipt.
    ``outstanding_human_requests`` counts retained valid requests regardless of
    freshness or state (a request never expires into a resolution).
    """
    observations = [project(v, now=now, source_class=source_class)
                    for v in (values if isinstance(values, (list, tuple)) else [])]
    counts = {state: 0 for state in sorted(STATES)}
    outstanding = 0
    for observation in observations:
        counts[observation["state"]] += 1
        outstanding += human_request_valid(observation.get("human_request"), wait_kind=_wait_kind(observation))
    return {"observations": observations, "counts": counts, "outstanding_human_requests": outstanding,
            "uncertain": not observations or bool(counts["unknown"])}
