"""Read-only turn/process execution evidence over rows the session producers already read.

No I/O, no persisted state. Source clocks are those the producer actually has: a turn lease's
``acquired_at``/``expires_at``, or the time a process liveness probe ran. A request never renews
them. Lease renewals are not dated by the store, heartbeats are never progress, PID alone is never
an identity, and nothing here names an owner the producer did not record.
"""
from __future__ import annotations

import hashlib

from hermes_cli.execution_observations import SOURCE_TTLS, ident, project

TURN_SOURCE = "turn.lease"
PROCESS_SOURCES = frozenset({"process.registry", "process.checkpoint"})
OWNED_SOURCES = frozenset({TURN_SOURCE}) | PROCESS_SOURCES


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
    cid = lease.get("conversation_id")
    digest = holder_digest(lease.get("holder"))
    generation = f"holder-sha256:{digest}" if digest else None
    # The raw holder never leaves this function: not in identity, revision or references.
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
    generation = (f"pid={pid}:start={start}" if type(pid) is int and pid > 0
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


def replace(row, sources, observations):
    """Append our observations, replacing only prior ones from ``sources``; never touch card/capacity ones."""
    previous = row.get("execution_observations")
    previous = previous if isinstance(previous, list) else []
    kept = [v for v in previous if not (isinstance(v, dict) and v.get("source") in sources)]
    if kept or observations:
        row["execution_observations"] = [*kept, *observations]
    else:
        row.pop("execution_observations", None)
