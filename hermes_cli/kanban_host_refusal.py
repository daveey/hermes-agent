"""A host fault that refuses a Kanban worker before any turn runs is not a card failure. [daveey]

hermes-hub 2026-11-01: a host config outage emptied ``model_routing.routes``. Every managed Kanban
worker the owner admitted then died in ``agent.managed_worker._construct_agent`` with
``ManagedPinRejectedError`` (an empty registry serves no pin), exited 1 and was booked ``crashed``:
failure_limit=2 sent unrelated cards to triage. The owner-unavailable neutral requeue
(``kanban_owner_unavailable``) refused them because the owner had admitted the run, and an admitted
run may have run a turn. These had not: agent construction precedes the first turn. The same outage
made the owner refuse other claims with ``storage_unavailable`` before admitting them; those are
requeued uncounted (``kanban_worker_client``), but nothing said the host was broken.

The fix is a typed proof, written by the process that saw the refusal and read back by the
dead-worker sweep, never a blanket pass for routing errors and never text matching:

- ``startup_proof`` (worker side, called only from ``session_kanban.record_start_failure``, i.e. from
  agent construction) returns a proof only for a ``ManagedPinRejectedError`` whose ``registry_empty``
  is set (the registry served no eligible subscription pin to any card) or a
  ``HostRoutingConfigError`` (``managed_config`` could not read or validate the host's registry). A
  registry that serves other pins (this card's pin is genuinely wrong), any other routing error and
  any failure after the agent started get no proof.
- the start failure then books ``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE`` with the proof on its
  ``worker_result``; ``proven`` accepts it for an admitted run only when that result is the run's sole
  result, under the run's claim, from the pid ``worker_bound`` recorded under that claim (not the
  submitter, not a later run). ``kanban_owner_unavailable.note`` then requeues it uncounted despite
  the admission, still bounded by ``NEUTRAL_STREAK_LIMIT``.
- ``preadmission_proof`` (submitter side, ``kanban_worker_client``) marks a run whose owner kept
  refusing it with ``storage_unavailable`` or ``capsule_observation_missing`` (both raised before the
  owner creates a session) before any admission. Such a run is requeued uncounted
  without it; the proof (the run's sole result, from its own submitter pid, nothing bound) only names
  the host fault for the notice, so a restart drain (an owner that is silent) files none.
- ``notify`` surfaces either host fault as ONE Kanban card per host on the board (agent-fixable, so
  never an Inbox card), commented at most once per ``NOTICE_WINDOW_SECONDS`` window however many
  cards are refused. A done card does not swallow a recurrence: the next round's key opens a fresh
  card (``kanban_systemic_crash``'s round keys; a done card keeps its own key).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)

PROOF_KEY = "host_refusal"
REGISTRY_EMPTY = "managed_registry_empty"
REGISTRY_INVALID = "managed_registry_invalid"
OWNER_STORAGE_UNAVAILABLE = "owner_storage_unavailable"
OWNER_CAPSULE_UNAVAILABLE = "owner_capsule_unavailable"
# A running worker's persistence found no answering owner (``WorkerPersistenceError: owner_unavailable``).
OWNER_UNREACHABLE = "owner_unreachable"
STARTUP_KINDS = frozenset({REGISTRY_EMPTY, REGISTRY_INVALID})
# ``kanban.run`` refusal reasons that name the owner host's own state (``kanban_worker_client``), each raised
# before the owner creates a session for the claim.
PREADMISSION_KINDS = {"storage_unavailable": OWNER_STORAGE_UNAVAILABLE,
                      "capsule_observation_missing": OWNER_CAPSULE_UNAVAILABLE}
NOTICE_WINDOW_SECONDS = 15 * 60
NOTICE_KEY_PREFIX = "host-refusal:"
NOTICE_EVENT = "host_refusal_notice"
AUTHOR = "kanban-dispatcher"


def startup_proof(exc) -> Optional[dict]:
    """The proof payload for an agent-construction failure caused by the host's routing registry
    (empty, or unreadable/invalid), else None. The cause chain is walked because agent construction
    may wrap the routing error; the first typed managed-routing refusal found decides."""
    from agent.model_routing_managed import HostRoutingConfigError, ManagedPinRejectedError
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, HostRoutingConfigError):
            return {"kind": REGISTRY_INVALID, "phase": "startup", "detail": str(exc)[:200]}
        if isinstance(exc, ManagedPinRejectedError):
            if exc.registry_empty is not True:
                return None
            return {"kind": REGISTRY_EMPTY, "phase": "startup", "model": str(exc.model or "")[:200],
                    "provider": str(exc.provider or "")[:100]}
        exc = exc.__cause__ or exc.__context__
    return None


def preadmission_proof(reason) -> Optional[dict]:
    """The proof payload for an owner that kept refusing an unadmitted claim with *reason*, when that
    reason is the host's own state (``PREADMISSION_KINDS``); None otherwise."""
    kind = PREADMISSION_KINDS.get(str(reason or ""))
    return {"kind": kind, "phase": "preadmission", "reason": str(reason)} if kind else None


def _run_events(conn, task_id, run_id, kind):
    out = []
    for (payload,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? ORDER BY id",
                                   (task_id, int(run_id), kind)):
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            data = None
        out.append(data if isinstance(data, dict) else {})
    return out


def proven(conn: sqlite3.Connection, task_id: str, run_id, claim_lock, *, admitted: bool = True,
           pid=None) -> Optional[dict]:
    """The run's host-refusal proof when the process that saw the refusal wrote it for this exact
    claim and nothing else was booked for the run; None otherwise (no proof, or one we cannot tie to it).

    Admitted: a startup proof from the interpreter ``worker_bound`` recorded under the claim.
    Not admitted: a pre-admission proof from the run's own submitter (*pid*, ``tasks.worker_pid``),
    with no interpreter ever bound."""
    from hermes_cli.kanban_owner_unavailable import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
    if run_id is None or not claim_lock:
        return None
    bound = _run_events(conn, task_id, run_id, "worker_bound")
    results = _run_events(conn, task_id, run_id, "worker_result")
    if len(results) != 1:
        return None
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    modern, selected = modern_receipt(conn, task_id, run_id, claim_lock)
    if modern and selected is None:
        return None
    (result,) = results
    proof = result.get(PROOF_KEY)
    if (result.get("claim_lock") != claim_lock or result.get("exit_code") != KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            or not isinstance(proof, dict)):
        return None
    if admitted:
        if not bound or bound[-1].get("claim_lock") != claim_lock:
            return None
        writer, phase, kinds = bound[-1].get("pid"), "startup", STARTUP_KINDS
    else:
        if bound or result.get("submitter") is not True:
            return None
        writer, phase, kinds = pid, "preadmission", frozenset(PREADMISSION_KINDS.values())
    if (writer is None or result.get("pid") != writer or proof.get("phase") != phase
            or proof.get("kind") not in kinds):
        return None
    return proof


def run_error(pid, proof: dict) -> str:
    """The requeued run's error text for a proven host refusal."""
    phase = "turn interrupted" if proof.get("phase") == "midrun" else "no turn ran"
    return f"pid {pid}: {_cause(proof)}; {phase}, requeued without counting a failure"


def _cause(proof: dict, task_id: Optional[str] = None) -> str:
    whose = f"card {task_id}'s" if task_id else "the"
    kind = proof.get("kind")
    if kind == REGISTRY_INVALID:
        return (f"the host's model-routing registry could not be read or validated when {whose} worker "
                f"started ({proof.get('detail') or 'invalid'})")
    if kind == OWNER_STORAGE_UNAVAILABLE:
        if proof.get("phase") == "midrun":
            return "the profile owner refused the running worker's persistence with storage_unavailable"
        claim = f"card {task_id}'s claim" if task_id else "the claim"
        return (f"the profile owner kept refusing {claim} with {proof.get('reason') or 'storage_unavailable'} "
                "(its runtime store or session policy could not be read)")
    if kind == OWNER_UNREACHABLE:
        return (f"{whose} running worker could not reach its profile owner to save the turn "
                "(owner_unavailable: the owner gateway stopped answering)")
    if kind == OWNER_CAPSULE_UNAVAILABLE:
        claim = f"card {task_id}'s claim" if task_id else "the claim"
        return (f"the profile owner kept refusing {claim} with capsule_observation_missing (no live run-bound "
                "capsule transport, or a profile policy it could not read made it require one)")
    pin = "/".join(p for p in (proof.get("provider"), proof.get("model")) if p) or "its pin"
    return f"the host's model-routing registry served no subscription pin when {whose} worker started ({pin})"


def _host() -> str:
    from hermes_cli import kanban_db as kb
    return kb._host_prefix().rstrip(":")


def notify(conn: sqlite3.Connection, task_id: str, proof: Optional[dict] = None) -> Optional[str]:
    """Surface a host refusal once per window per host; returns the notice card id. Never raises:
    it runs inside the dead-worker sweep, whose booking must not depend on it (a failure rolls back
    only the notice's own savepoint or transaction)."""
    try:
        return _notify(conn, task_id, proof or {})
    except (sqlite3.Error, ValueError, RuntimeError):
        logger.warning("kanban: could not file the host refusal notice", exc_info=True)
        return None


def _notify(conn, task_id, proof):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_systemic_crash import _MAX_ROUNDS, _open_card
    host = _host()
    base = NOTICE_KEY_PREFIX + host
    window = int(time.time()) // NOTICE_WINDOW_SECONDS
    phase = "the turn was interrupted" if proof.get("phase") == "midrun" else "no turn ran"
    line = f"{host}: {_cause(proof, task_id)}; {phase} and the card was requeued without counting a failure."
    marker = {"window": window, "task_id": task_id, "kind": proof.get("kind")}
    # The lookup runs inside the write transaction (the sweep's IMMEDIATE one, or our own), so two
    # sweeps cannot both find no card and open two.
    with kb.write_txn(conn, allow_nested=True):
        for n in range(1, _MAX_ROUNDS + 1):
            key = base if n == 1 else f"{base}:r{n}"
            existing, reusable = _open_card(conn, key)
            if existing and not reusable:
                continue  # done: a recurrence is the next round; the done card keeps its key
            if existing:
                noticed = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND json_extract(payload, '$.window') = ? "
                    "LIMIT 1", (existing, NOTICE_EVENT, window)).fetchone()
                if not noticed:
                    kb.add_comment(conn, existing, AUTHOR, f"Again: {line}")
                    kb._append_event(conn, existing, NOTICE_EVENT, marker)
                return existing
            body = (f"{line}\n\nA host fault refuses Kanban workers on this host before or during a turn: an empty, "
                    "unreadable or invalid `model_routing` registry when a worker starts, a profile owner whose "
                    "runtime store or session policy cannot be read (`storage_unavailable`), or one that refuses "
                    "the claim without capsule transport (`capsule_observation_missing`). The refused cards are "
                    "requeued uncounted (up to the owner-unavailable streak limit, then they count), and this card "
                    "is commented at most once per 15 minutes while it lasts.\n\n"
                    "Fix: inspect the profile owner's storage health and config.yaml (the `model_routing` "
                    "registry, model and session policy), restore the failing dependency, then confirm a "
                    "refused card runs.")
            tid = kb.create_task(conn, title=f"{host}: host config fault, Kanban workers refused"[:120],
                                 body=body, assignee="default", created_by=AUTHOR, priority=80,
                                 idempotency_key=key)
            kb._append_event(conn, tid, NOTICE_EVENT, marker)
            return tid
    logger.warning("kanban: host refusal notice rounds exhausted for %s", host)
    return None
