"""Recognize an owner fault in the bound worker's terminal persistence receipt. [daveey]

Two exact managed-worker exception envelopes are owner faults, never the card's:
``storage_unavailable`` (the owner answered and refused: its store could not be read) and
``owner_unavailable`` (the worker could not reach its owner at all: ``WorkerRPC`` rediscovery found no
answering gateway, e.g. one wedged after an aborted core-update drain; hermes-hub 2026-11-13 20:06Z).
Its sibling, where the redial reached the gateway but the resend still got no receipt
(``worker_receipt_timeout``) or the owner dropped the link again (``owner_link_closed``), is the same
owner-silent fault (``runtime_session_store.exhausted_resend_code``).
"""
from hermes_cli.kanban_host_refusal import OWNER_STORAGE_UNAVAILABLE, OWNER_UNREACHABLE, _run_events

_ENVELOPE = "Worker failed: WorkerPersistenceError: "
# Terminal persistence wire code -> host-refusal kind. Worker-local codes (outbox_full, lost_receipt,
# stale_generation, ...) stay card failures.
_KINDS = {"storage_unavailable": OWNER_STORAGE_UNAVAILABLE, "owner_unavailable": OWNER_UNREACHABLE,
          "worker_receipt_timeout": OWNER_UNREACHABLE, "owner_link_closed": OWNER_UNREACHABLE}


def _proof(result):
    """The proof for an exit-1 receipt whose whole last line is one owner-fault envelope, else None."""
    output = result.get("last_output")
    if result.get("exit_code") != 1 or not isinstance(output, str) or not output.startswith(_ENVELOPE):
        return None
    reason = output[len(_ENVELOPE):]
    kind = _KINDS.get(reason)
    return {"kind": kind, "phase": "midrun", "reason": reason} if kind else None


def proven(conn, task_id, run_id, claim_lock):
    """No shared-log fallback: only the newest result from this run's bound interpreter.

    A failed turn can write its own result before the outer worker writes its final
    exception. Accept that real two-receipt shape, but never search past a newer result.
    The complete terminal line is the managed-worker exception envelope, not arbitrary
    agent output containing the name of a storage error.
    """
    if conn is None or run_id is None or not claim_lock:
        return None
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    modern, selected = modern_receipt(conn, task_id, run_id, claim_lock)
    if modern:
        return None if selected is None else _proof(selected[1])
    bound = _run_events(conn, task_id, run_id, "worker_bound")
    if not bound or bound[-1].get("claim_lock") != claim_lock or bound[-1].get("pid") is None:
        return None
    results = _run_events(conn, task_id, run_id, "worker_result")
    if not results:
        return None
    result = results[-1]
    if result.get("claim_lock") != claim_lock or result.get("pid") != bound[-1]["pid"]:
        return None
    return _proof(result)
