"""Read-only turn/process execution evidence over rows the session producers already read.

No I/O, no persisted state. Source clocks are those the producer actually has: a turn lease's
``acquired_at``/``expires_at``, or the time a process liveness probe ran. A request never renews
them. Lease renewals are not dated by the store, heartbeats are never progress, PID alone is never
an identity, and nothing here names an owner the producer did not record.
"""
from __future__ import annotations

import hashlib
import logging
import math

from hermes_cli.execution_observations import SOURCE_TTLS, ident, project

_log = logging.getLogger(__name__)

TURN_SOURCE = "turn.lease"
PROCESS_SOURCES = frozenset({"process.registry", "process.checkpoint"})
SUBAGENT_SOURCE = "subagent.activity"
# Replace old producer records during mixed-version rollout, never treat their ids as child ids.
SUBAGENT_SOURCES = frozenset({SUBAGENT_SOURCE, "subagent.registry"})
OWNED_SOURCES = frozenset({TURN_SOURCE}) | PROCESS_SOURCES | SUBAGENT_SOURCES


def profile_for_state_path(path):
    """Profile of a ``<home>/state.db`` or ``<home>/profiles/<name>/state.db``-style sibling file."""
    parent = path.parent
    return parent.name if parent.parent.name == "profiles" else "default"


_HOLDER_DOMAIN = b"hermes.execution-observations.turn-lease-holder.v1\x00"


def holder_digest(holder):
    """Non-reversible lease-instance id: SHA-256 over a fixed domain tag, NUL, then the exact holder bytes.

    The holder is the compare-and-swap credential of ``turn.renew``/``turn.release``, so it is never
    emitted. Equal holders (one acquisition) digest equally; differing holders do not alias. Limit: a
    holder is low-entropy (pid, pid namespace, turn id), so the digest resists casual reading, not a
    targeted offline guess. Anything not an exact identifier (blank, padded, non-text) has no digest.
    """
    if not ident(holder):
        return None
    return hashlib.sha256(_HOLDER_DOMAIN + holder.encode("utf-8", "surrogatepass")).hexdigest()


def _identity(kind, work_id, gateway, profile, session, generation):
    identity = {"kind": kind, "work_id": work_id}
    for key, value in (("gateway", gateway), ("profile", profile), ("session", session),
                       ("generation", generation)):
        if value is not None:
            identity[key] = value
    return identity


def _owner(gateway, profile, session):
    return {k: v for k, v in (("gateway", gateway), ("profile", profile), ("session", session))
            if v is not None}


def turn(lease, *, gateway, profile, now):
    """Lease row: ``conversation_id, holder, acquired_at, expires_at``.

    The store has no generation counter. Each acquisition inserts a new row with a unique holder
    token (``pid=..:turn=..`` or ``..:nonce=..``), so its digest identifies the lease instance.
    A missing/padded token leaves the generation absent and the projection unknown.
    """
    digest = holder_digest(lease.get("holder"))
    return turn_from_digest(lease, digest=digest, gateway=gateway, profile=profile, now=now)


def turn_from_digest(lease, *, digest, gateway, profile, now):
    """Native capture supplies only the one-way digest, never its CAS credential."""
    cid = lease.get("conversation_id")
    generation = f"holder-sha256:{digest}" if digest else None
    # The raw holder never leaves the legacy adapter or enters a captured fact.
    reference = f"session_turn_leases/{cid}/{generation}"
    raw = {"version": 1, "identity": _identity("turn", cid, gateway, profile, cid, generation),
           "owner": _owner(gateway, profile, cid), "state": "running", "source": TURN_SOURCE,
           "source_revision": f"{reference}@{lease.get('acquired_at')}", "evidence_refs": [reference],
           "observed_at": lease.get("acquired_at"), "valid_until": lease.get("expires_at"),
           "last_activity": None, "last_progress": None, "last_progress_kind": None,
           "last_progress_ref": None, "wait": None}
    return project(raw, now=now, source_class="sidebar")


def process(entry, *, gateway, profile, session, now):
    """Process evidence: ``id``, ``pid``, ``host_start_time`` (kernel birth), ``observed_at`` (probe time),
    ``source``. Identity is the registry process id plus its birth-qualified host process."""
    pid, start = entry.get("pid"), entry.get("host_start_time")
    # Only a probed host pid matches a kernel birth. A sandbox/container pid lives in another pid
    # namespace, so a start tick it carries proves nothing here: no generation, hence unknown.
    host = entry.get("pid_scope", "host") == "host"
    generation = (f"pid={pid}:start={start}" if host and type(pid) is int and pid > 0
                  and type(start) is int and start > 0 else None)
    work_id = entry.get("id")
    reference = f"{entry.get('source')}/{work_id}"
    observed = entry.get("observed_at")
    raw = {"version": 1, "identity": _identity("process", work_id, gateway, profile, session, generation),
           "owner": _owner(gateway, profile, session), "state": "running", "source": entry.get("source"),
           "source_revision": f"{reference}@{observed}", "evidence_refs": [reference],
           "observed_at": observed,
           "valid_until": observed + SOURCE_TTLS["sidebar"] if isinstance(observed, (int, float)) else None,
           "last_activity": None, "last_progress": None, "last_progress_kind": None,
           "last_progress_ref": None, "wait": None}
    return project(raw, now=now, source_class="sidebar")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def activity_clock(record):
    """The live child's own ``last_activity_ts`` (``get_activity_summary``), or None when unavailable."""
    agent = record.get("agent")
    try:
        summary = agent.get_activity_summary()
        value = summary.get("last_activity_ts", summary.get("last_activity_at")) if isinstance(summary, dict) else None
    except (AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        # a test double or half-built child: no clock, never an error
        _log.debug("child activity summary unavailable", exc_info=True)
        return None
    return value if _finite(value) else None


def registry_subagent(record, *, gateway, profile, now):
    """Adapt registry records without inventing child ids for legacy/half-built agents."""
    agent = record.get("agent")
    parent = getattr(agent, "_parent_session_id", None)
    parent = parent if ident(parent) else record.get("owner_agent_session_id")
    return subagent({"child_session_id": getattr(agent, "session_id", None),
                     "parent_session_id": parent, "activity_at": activity_clock(record),
                     "started_at": record.get("started_at"), "live": record.get("status") == "running"},
                    gateway=gateway, profile=profile, now=now)


def subagent(evidence, *, gateway, profile, now):
    """Pure canonical child projection shared by registry and persisted producers.

    Spawn times differ between adapters: they are fallback clocks, never identity. Equal child
    activity clocks yield identical records. Registry/delegation ids cannot enter canonical refs.
    Activity is not progress; missing child identity is unknown, never a fabricated PID/id.
    """
    work_id, owner = evidence.get("child_session_id"), evidence.get("parent_session_id")
    started, activity = evidence.get("started_at"), evidence.get("activity_at")
    generation = f"session={work_id}" if ident(work_id) else None
    reference = f"{SUBAGENT_SOURCE}/{work_id}"
    if not (_finite(activity) and activity <= now):
        activity = None
    activity = float(activity) if activity is not None else None
    observed = activity if activity is not None else (float(started) if _finite(started) else None)
    raw = {"version": 1, "identity": _identity("subagent", work_id, gateway, profile,
                                               owner if ident(owner) else None, generation),
           "owner": _owner(gateway, profile, owner if ident(owner) else None), "state": "running",
           "source": SUBAGENT_SOURCE, "source_revision": f"{reference}@{observed}", "evidence_refs": [reference],
           "observed_at": observed,
           "valid_until": observed + SOURCE_TTLS["sidebar"] if observed is not None else None,
           "last_activity": activity, "last_progress": None, "last_progress_kind": None,
           "last_progress_ref": None, "wait": None}
    if evidence.get("live") is not True:
        raw.update(state="unknown", unknown_reason="subagent_not_running")
    elif not (ident(owner) and ident(work_id)):
        raw.update(state="unknown", unknown_reason="missing_subagent_child_or_parent")
    return project(raw, now=now, source_class="sidebar")


def replace(row, sources, observations):
    """Append our observations, replacing only prior ones from ``sources``; never touch card/capacity ones."""
    previous = row.get("execution_observations")
    previous = previous if isinstance(previous, list) else []
    kept = [v for v in previous if not (isinstance(v, dict) and v.get("source") in sources)]
    if kept or observations:
        row["execution_observations"] = [*kept, *observations]
    else:
        row.pop("execution_observations", None)
