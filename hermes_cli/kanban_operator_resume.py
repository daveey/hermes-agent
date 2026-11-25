"""Operator resume of a ready card the respawn guard holds on ``active_pr``. [daveey]

``hermes kanban resume <id> --reason ...``: the supported handoff when the guard holds
an owner that should continue its own PR (for example a review finished on another
gateway with no readable result here). Operator lineage only: a Kanban worker (its
process or its tool shells) is refused, so an implementer cannot resume itself.

One continuation: the receipt names the card's latest run by portable identity and
lapses as soon as a newer run is claimed, so a crash of the resumed run guards again.
A hermes-sync core-update requeue is a pause, not a run (:mod:`kanban_core_update_requeue`):
it neither spends the receipt nor stops one recorded after it. It never authorizes a merge.
"""
from __future__ import annotations

import os
import time

EVENT = "operator_resumed"
_WORKER_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")


class ResumeRefused(ValueError):
    pass


def worker_lineage() -> str | None:
    """The worker marker this process carries, or None for an operator."""
    for key in _WORKER_ENV:
        value = (os.environ.get(key) or "").strip()
        if value:
            return f"{key}={value}"
    return None


def _latest_run(conn, task_id):
    # Chronological, not by local id: replica run ids are not time-ordered.
    return conn.execute("SELECT * FROM task_runs WHERE task_id = ? "
                        "ORDER BY COALESCE(started_at, 0) DESC, id DESC LIMIT 1", (task_id,)).fetchone()


def _identity(run):
    if run is None:
        return None
    if "uid" in run.keys() and run["uid"]:
        return {"run_uid": run["uid"]}
    return {"run_id": run["id"]}


def _tip(conn, task_id):
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]


def resume(conn, task_id: str, *, reason: str, actor: str) -> dict:
    """Record one operator continuation for a guarded READY card; raises ResumeRefused."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    from hermes_cli.kanban_wake_check import foreign_copy

    marker = worker_lineage()
    if marker:
        raise ResumeRefused(f"operator-only: refused under worker lineage ({marker})")
    from hermes_cli.kanban_home_transfer import frozen
    if frozen(conn, task_id):
        raise ResumeRefused("home transfer owns this card; resume deferred")
    reason = (reason or "").strip()
    if not reason:
        raise ResumeRefused("resume requires --reason")
    actor = (actor or "").strip() or "operator"
    tip = _tip(conn, task_id)
    # The guard may consult GitHub: evaluate it before the write lock, then fence on the event tip.
    guard = check_respawn_guard(conn, task_id)
    from hermes_cli.kanban_observed_pr_hold import observed
    if guard != "active_pr" and not (guard is None and observed(conn, task_id)):
        raise ResumeRefused(f"resume requires an active_pr hold (guard: {guard or 'none'})")
    with write_txn(conn):
        if frozen(conn, task_id):
            raise ResumeRefused("home transfer owns this card; resume deferred")
        task = kb.get_task(conn, task_id)
        if (task is None or task.status != "ready" or task.current_run_id is not None
                or task.claim_lock or task.claim_expires):
            raise ResumeRefused("resume requires an unclaimed READY card")
        if foreign_copy(conn, task_id):
            raise ResumeRefused("resume must run on the card's home gateway")
        if _tip(conn, task_id) != tip:
            raise ResumeRefused("card changed while checking the guard; retry")
        payload = {"via": "operator-resume", "actor": actor, "reason": reason[:2000],
                   "after_run": _identity(_latest_run(conn, task_id)),
                   "scope": "one_continuation", "merge_authorized": False}
        kb._append_event(conn, task_id, EVENT, payload)
        # Operator comments carry no writer run, so a PR named in the reason is FYI to the guard.
        kb.add_comment(conn, task_id, actor, f"RESUMED: {reason}")
    return payload


def _still_current(conn, task_id, after) -> bool:
    """``after`` names the newest run once core-update requeues (pauses) are passed over.

    Walk newest first: a requeue may follow the named run or be the named run itself; the
    first real run that is not the named one (a claim, crash or other reclaim) spends it."""
    from hermes_cli.kanban_core_update_requeue import _skipped
    from hermes_cli.kanban_dependency_review import _checkpoint_matches_run

    for run in conn.execute("SELECT * FROM task_runs WHERE task_id = ? "
                            "ORDER BY COALESCE(started_at, 0) DESC, id DESC", (task_id,)):
        if isinstance(after, dict) and _checkpoint_matches_run(after, run):
            return True
        if not _skipped(conn, run):
            return False
    return after is None


def resumed_after(conn, task_id: str, since: int) -> bool:
    """A receipt strictly after the newest PR comment whose run is still the latest run.

    "Latest" skips hermes-sync core-update requeues, as ``released_after`` does: a requeue
    is a pause, so it neither spends the receipt nor stops one recorded right after it."""
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    for event in conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = ?",
                              (task_id, EVENT)):
        data = kb._json_dict(event["payload"])
        at = kb._to_epoch(event["created_at"])
        if (at is None or at <= since or at > now or data.get("via") != "operator-resume"
                or data.get("scope") != "one_continuation" or data.get("merge_authorized") is not False):
            continue
        after = data.get("after_run", False)
        if (after is None or isinstance(after, dict)) and _still_current(conn, task_id, after):
            return True
    return False


def cmd(args) -> int:
    """``hermes kanban resume <id> --reason ...``."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban import _profile_author
    from hermes_cli.kanban_output import _err

    marker = worker_lineage()
    if marker:  # refuse before opening the board
        return _err(f"kanban resume: operator-only: refused under worker lineage ({marker})", 2)
    with kbc.connect_closing() as conn:
        try:
            resume(conn, args.task_id, reason=args.reason, actor=_profile_author())
        except ResumeRefused as exc:
            return _err(f"kanban resume: {exc}", 2)
    print(f"Resumed {args.task_id}: one continuation recorded ({EVENT})")
    return 0
