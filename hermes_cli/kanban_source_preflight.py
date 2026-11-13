"""workspace_source v1 ending preflight (core PR-2; v3 §2 "Core side", v4 S3/S6, v4.1 A7): compute the
``source_receipt`` of a voluntary ending BEFORE its write transaction and stage it for ``with_source_receipt``.

``source_receipt_preflight`` wraps ``complete_task``, ``request_changes`` and (through ``publication_guard``)
``block_task``, ``schedule_task``, ``request_review`` and ``continue_task``. For a run that attached (local
``kanban_source_runs``):

* clean tree, ``HEAD == manifest.commit`` and ``ls-remote`` agreeing: ``unchanged``, no publish (S3);
* otherwise publish (v2 §4 steps 1-8) and return ``published`` with the Board's new revision;
* any refusal, the preflight's own timeout (``preflight_timeout_s``), a disabled feature or anything unreadable:
  ``retained`` with a reason. The ending always proceeds (S6): only PR1133's ``unpublished_workspace`` refuses one.

Involuntary endings (crash, timeout, kernel goal parks) are not wrapped: they stage nothing, so they send no receipt
and custody stays with this host (``retained``). The receipt is computed with no SQLite transaction open; in the
ending's transaction ``with_source_receipt`` only reads it.
"""

from __future__ import annotations

import contextvars
import logging
import sqlite3
from functools import wraps
from typing import Any, Callable, Optional

from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_source_receipt as carrier

_log = logging.getLogger(__name__)
# (task_id, run_id, [(code, detail)]) for the ending being preflighted: the agent-facing ``unpublished_workspace``
# refusal names the publish refusal, so its fix (e.g. registering the clone) is visible, not only in an event.
_REFUSALS: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("kanban_source_refusals", default=None)
_HINTS = {"source_clone_unregistered": "register the worktree's clone under kanban.workspace_source.clones "
                                       "({\"<owner>/<name>\": \"<clone root>\"})"}


def _retained(run: board_calls.AttachedRun, reason: str) -> dict:
    return {"run_uid": run.run_uid, "state": "retained", "manifest_revision": run.manifest_revision,
            "commit": None, "reason": reason[:200]}


def _task(conn: sqlite3.Connection, task_id: str) -> Any:
    from hermes_cli import kanban_db as kb
    return kb.get_task(conn, task_id)


def compute_receipt(conn: sqlite3.Connection, task_id: str, run: board_calls.AttachedRun, *,
                    publish: Optional[bool] = None) -> dict:
    """The receipt for ``run`` ending now. Never raises (anything unexpected is ``retained``)."""
    from hermes_cli import kanban_source_publish as pub
    from hermes_cli.kanban_workspace_source import settings
    cfg = settings()
    if not cfg.enabled:
        return _retained(run, "source_disabled")
    if conn.in_transaction:
        return _retained(run, "preflight_in_transaction")
    deadline = pub._Deadline(cfg.preflight_timeout_s)
    try:
        manifest = run.manifest
        seen = pub.observe(deadline, str(run.path or ""), remote=False)
        if manifest and seen.clean and seen.head == manifest["commit"] and seen.branch and \
                f"refs/heads/{seen.branch}" == manifest["branch"] and \
                pub.ls_remote(deadline, str(run.path), seen.branch) == manifest["commit"]:
            return {"run_uid": run.run_uid, "state": "unchanged", "manifest_revision": int(manifest["manifest_revision"]),
                    "commit": manifest["commit"]}
        if not (cfg.publish_on_park if publish is None else publish):
            return _retained(run, "publish_disabled")
        from hermes_cli import kanban_db_sync as sync
        machine = sync._state(conn)["gateway_id"]
        done = pub.publish(conn, _task(conn, task_id), run, cfg, deadline, machine=machine)
        board_calls.update_run(conn, run.run_id, manifest=done.manifest, manifest_revision=done.revision)
        if done.manifest.get("published_by", {}).get("run_uid") != run.run_uid:
            return {"run_uid": run.run_uid, "state": "unchanged", "manifest_revision": done.revision,
                    "commit": done.commit}
        return {"run_uid": run.run_uid, "state": "published", "manifest_revision": done.revision,
                "commit": done.commit}
    except pub.PublishRefused as exc:
        _log.warning("workspace_source: %s not published (%s); custody stays here", task_id, exc)
        _event(conn, task_id, run, exc.code, exc.detail)
        _note_refusal(task_id, run.run_id, exc.code, exc.detail)
        return _retained(run, f"publish_error={exc.code}")
    except Exception as exc:  # health: allow BLE001 -- a preflight must never refuse an ending; fail closed (retained)
        _log.warning("workspace_source: preflight of %s failed", task_id, exc_info=True)
        return _retained(run, f"preflight_error={type(exc).__name__}")


def _note_refusal(task_id: str, run_id: int, code: str, detail: str) -> None:
    scope = _REFUSALS.get()
    if scope and scope[0] == task_id and scope[1] == run_id:
        scope[2].append((code, detail))


def refusal_hint(task_id: str, run_id: Optional[int]) -> str:
    """`` (workspace_source publish refused: <code>: <detail>; <fix>)`` for this run's preflight, else ``""``."""
    scope = _REFUSALS.get()
    if not scope or scope[0] != task_id or scope[1] != run_id or not scope[2]:
        return ""
    code, detail = scope[2][-1]
    text = f"{code}: {detail[:1000]}" if detail else code
    fix = _HINTS.get(code)
    return f" (workspace_source publish refused: {text}" + (f"; {fix})" if fix else ")")


def _event(conn: sqlite3.Connection, task_id: str, run: board_calls.AttachedRun, code: str, detail: str) -> None:
    from hermes_cli import kanban_db as kb
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "workspace_source_publish_refused",
                             {"code": code, "detail": detail[:300], "run_uid": run.run_uid}, run_id=run.run_id)
    except sqlite3.Error:
        _log.info("workspace_source: could not record the publish refusal of %s", task_id, exc_info=True)


def receipt_for_current_run(conn: sqlite3.Connection, task_id: str, *,
                            publish: Optional[bool] = None) -> tuple[Optional[int], Optional[dict]]:
    """``(run_id, receipt)`` for the card's running, attached run; ``(run_id, None)`` when it did not attach."""
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row[0] != "running" or row[1] is None:
        return None, None
    run = board_calls.attached_run(conn, row[1])
    if run is None or run.state != "attached" or board_calls.board_lacks_source():
        return row[1], None
    return row[1], compute_receipt(conn, task_id, run, publish=publish)


def source_receipt_preflight(fn: Callable) -> Callable:
    """Stage the ending's receipt for its one status-leaving UPDATE (``with_source_receipt``). Nested endings of the
    same run (an ending that calls another) reuse the outer receipt; a kernel park stages nothing (involuntary)."""
    @wraps(fn)
    def wrapped(conn, task_id, *args, **kwargs):
        from hermes_cli.kanban_workspace_publication import _kernel_park
        if _kernel_park.get() == task_id:
            return fn(conn, task_id, *args, **kwargs)
        row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row[0] is None or carrier.staged_receipt(task_id, row[0]) is not None:
            return fn(conn, task_id, *args, **kwargs)
        token = _REFUSALS.set((task_id, int(row[0]), []))
        try:
            _run_id, receipt = receipt_for_current_run(conn, task_id)
            if receipt is None:
                return fn(conn, task_id, *args, **kwargs)
            with carrier.staged(task_id, int(row[0]), receipt):
                return fn(conn, task_id, *args, **kwargs)
        finally:
            _REFUSALS.reset(token)
    return wrapped
