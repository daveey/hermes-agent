"""Recognize an owner-store refusal in the bound worker's terminal receipt. [daveey]"""
from hermes_cli.kanban_host_refusal import OWNER_STORAGE_UNAVAILABLE, _run_events


def proven(conn, task_id, run_id, claim_lock):
    """No shared-log fallback: only the newest result from this run's bound interpreter.

    A failed turn can write an empty result before the outer worker writes its final
    exception. Accept that real two-receipt shape, but never search past a newer result.
    The complete terminal line is the managed-worker exception envelope, not arbitrary
    agent output containing the name of a storage error.
    """
    if conn is None or run_id is None or not claim_lock:
        return None
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    modern, selected = modern_receipt(conn, task_id, run_id, claim_lock)
    if modern:
        if selected is None:
            return None
        result = selected[1]
        if (result.get('exit_code') != 1
                or result.get('last_output') != 'Worker failed: WorkerPersistenceError: storage_unavailable'):
            return None
        return {'kind': OWNER_STORAGE_UNAVAILABLE, 'phase': 'midrun', 'reason': 'storage_unavailable'}
    bound = _run_events(conn, task_id, run_id, "worker_bound")
    if not bound or bound[-1].get("claim_lock") != claim_lock or bound[-1].get("pid") is None:
        return None
    results = _run_events(conn, task_id, run_id, "worker_result")
    if not results:
        return None
    result = results[-1]
    if (result.get("claim_lock") != claim_lock or result.get("pid") != bound[-1]["pid"]
            or result.get("exit_code") != 1
            or result.get("last_output") != "Worker failed: WorkerPersistenceError: storage_unavailable"):
        return None
    return {"kind": OWNER_STORAGE_UNAVAILABLE, "phase": "midrun", "reason": "storage_unavailable"}
