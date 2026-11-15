"""Monitor snapshots and pending generations share one atomic jobs-store write."""
from uuid import uuid4

from cron import jobs


def _validate_state(state):
    from cron.monitor import hash_monitor_output
    if (not isinstance(state, dict) or state.get("outbox_version") != 1
            or not isinstance(state.get("pending_changes"), list)
            or "last_output" not in state or "last_output_hash" not in state):
        raise ValueError("monitor outbox: corrupt state schema")
    old, digest = state["last_output"], state["last_output_hash"]
    if not isinstance(old, str) or (digest is None and old != "") or (
            digest is not None and digest != hash_monitor_output(old)):
        raise ValueError("monitor outbox: corrupt baseline snapshot")
    ids = set()
    for change in state["pending_changes"]:
        if (not isinstance(change, dict)
                or not isinstance(change.get("id"), str) or not change["id"]
                or change["id"] in ids
                or change.get("old") != old
                or not isinstance(change.get("new"), str)
                or type(change.get("first_run")) is not bool
                or change["first_run"] != (digest is None)
                or type(change.get("attempts")) is not int or change["attempts"] < 0
                or change.get("hash") != hash_monitor_output(change["new"])):
            raise ValueError("monitor outbox: corrupt pending snapshot")
        ids.add(change["id"])
        old, digest = change["new"], change["hash"]


def _state(job):
    from cron.monitor import _snapshot_path, hash_monitor_output
    state = job.get("monitor_state")
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise ValueError("monitor outbox: corrupt state")
    if "outbox_version" not in state:
        if "pending_changes" in state or "last_output" in state:
            raise ValueError("monitor outbox: missing schema version")
        digest = state.get("last_output_hash")
        try:
            old = _snapshot_path(job["id"]).read_bytes().decode("utf-8") if digest is not None else ""
        except (OSError, UnicodeError) as exc:
            raise ValueError("monitor outbox: missing or unreadable legacy snapshot") from exc
        if digest is not None and digest != hash_monitor_output(old):
            raise ValueError("monitor outbox: corrupt legacy snapshot")
        state = dict(state, outbox_version=1, last_output=old,
                     last_output_hash=digest, pending_changes=[])
        job["monitor_state"] = state
    _validate_state(state)
    return state


def stage(job_id: str, output: str):
    """Queue observed changes and return the oldest unacknowledged generation."""
    from cron.monitor import hash_monitor_output

    def apply(records, index, job):
        state = _state(job)
        pending = state["pending_changes"]
        previous = pending[-1]["hash"] if pending else state["last_output_hash"]
        digest = hash_monitor_output(output)
        if previous != digest:
            old = pending[-1]["new"] if pending else state["last_output"]
            pending.append({"id": uuid4().hex, "old": old, "new": output,
                            "hash": digest, "first_run": previous is None, "attempts": 0})
        if pending:
            pending[0]["attempts"] += 1  # one more offer to the agent: observable retries
        jobs.save_jobs(records)
        return dict(pending[0]) if pending else None

    missing = object()
    result = jobs._with_job(job_id, apply, missing=missing)
    if result is missing:
        raise ValueError("monitor outbox: missing job")
    return result


def acknowledge(job_id: str, change_id: str) -> bool:
    """Consume only this successful generation, not a newer observation."""
    return _consume(job_id, change_id)


def acknowledge_success(job: dict, result: dict, final_response: str, failure_marker) -> bool:
    """Acknowledge the run's offered generation iff the agent produced a successful answer.

    Called by the owner's ``run_job`` right after the agent returns and BEFORE output save or
    notification delivery, so a delivery failure never replays an agent that already handled the
    change. Blank, incomplete (including a max-iteration handoff), interrupted and
    ``[CRON_FAILURE]`` answers keep the generation pending; ``[SILENT]`` is a success. A cancel
    that arrives after the agent returned does not undo this: the agent did handle the change.
    """
    change_id = job.get("_monitor_change_id")
    if (not change_id or not final_response.strip() or result.get("completed") is False
            or result.get("interrupted") or failure_marker(final_response) is not None):
        return False
    return acknowledge(job["id"], change_id)


def discard(job_id: str, change_id: str, reason: str) -> bool:
    """Explicitly abandon one generation, preserving a distinct audit record."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("monitor outbox: discard reason required")
    return _consume(job_id, change_id, reason=reason)


def _consume(job_id, change_id, *, reason=None):
    def apply(records, index, job):
        state = _state(job)
        pending = state["pending_changes"]
        if not pending or pending[0]["id"] != change_id:
            return False
        done = pending.pop(0)
        if reason is not None:
            state["last_discard"] = {"change_id": change_id, "reason": reason,
                                     "at": jobs._hermes_now().isoformat()}
        state.update(last_output=done["new"], last_output_hash=done["hash"],
                     last_changed_at=jobs._hermes_now().isoformat())
        jobs.save_jobs(records)
        return True

    missing = object()
    result = jobs._with_job(job_id, apply, missing=missing)
    if result is missing:
        raise ValueError("monitor outbox: missing job")
    return result
