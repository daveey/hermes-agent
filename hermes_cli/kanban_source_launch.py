"""workspace_source v1 (core PR-2): from a resolved workspace to a started worker, for every dispatched card.

Moved out of ``kanban_db_dispatch._dispatch_lane_task`` (file-size ratchet) and extended with the source steps:

1. ``source_attach`` after the resolver and BEFORE spawn (v3 §6): a source card's run joins custody; a legacy worktree
   card's creator adopts it (``legacy_adopt``, v4 S4). A refused attach releases the grant unused and spawns nothing.
2. Nothing renews the lease between attach and spawn (v4.1 R2-a; pinned by a spy test).
3. Any abort between attach and the worker start (admission refusal, model tier, spawn raising a typed not-started
   error) PRE-EMPTS ``_record_task_failure``: it releases first with ``source_receipt{state: unchanged, abort:
   pre_spawn}`` (sync ``/leases release``, then the outbox release as fallback; first wins per run_uid), so the Board
   can restore custody and still counts the failure. A failure once a process may exist kills the worker and sends
   ``retained`` (R2-a post-pid).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from typing import Any, Callable, Optional

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_workspace_source as ws

_log = logging.getLogger(__name__)

_NO_SNAPSHOT = object()
_GRANTS: dict[str, Any] = {}


class SpawnNotStarted(RuntimeError):
    """A ``spawn_fn`` refusal raised before any process existed (the only spawn error a pre-spawn restore trusts)."""


def _not_started_types() -> tuple[type, ...]:
    from tools.process_registry import RestartSafeScopeUnavailable
    return (SpawnNotStarted, RestartSafeScopeUnavailable)


# --- grant bookkeeping ---------------------------------------------------------------------------------------------

def note_grant(task_id: str, snapshot: Optional[ws.SourceSnapshot]) -> None:
    _GRANTS[task_id] = _NO_SNAPSHOT if snapshot is None else snapshot


def _pop_grant(task_id: str) -> Any:
    return _GRANTS.pop(task_id, _NO_SNAPSHOT)


def drop_grant(task_id: str) -> None:
    _GRANTS.pop(task_id, None)


def old_board_grant(conn: sqlite3.Connection, claimed: Any) -> bool:
    """This grant came from an old Board (v4 S2): a primary grant asked with ``source_protocol`` came back with no
    snapshot keys (detected now), or an earlier grant already showed it (this process then stops sending the
    protocol, so every later grant lacks the keys too and its card must still never take the legacy path)."""
    if sync.board_mode(conn) != "primary" or not ws.settings().enabled:
        return False
    if not board_calls.board_lacks_source():
        board_calls.mark_board_lacks_source(f"grant of {claimed.id} carried no source snapshot")
    return True


# --- observation ---------------------------------------------------------------------------------------------------

def _git(path: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def observe(path: Optional[str], branch: Optional[str]) -> dict:
    """``{head, branch, clean}`` of a worktree (all None/False when it cannot be read)."""
    if not path:
        return {"head": None, "branch": None, "clean": False}
    status = _git(path, "status", "--porcelain", "--untracked-files=all")
    return {"head": _git(path, "rev-parse", "--verify", "-q", "HEAD"),
            "branch": _git(path, "symbolic-ref", "-q", "--short", "HEAD"), "clean": status == ""}


# --- attach --------------------------------------------------------------------------------------------------------

class AttachRefused(Exception):
    def __init__(self, code: str, detail: str, *, uncertain: bool = False):
        self.code, self.detail, self.uncertain = code, detail, uncertain
        super().__init__(f"{code}: {detail}")


def _me(conn: sqlite3.Connection) -> Optional[str]:
    from hermes_cli.kanban_source_dispatch import _me as me
    return me(conn)


def _attach_plan(conn: sqlite3.Connection, claimed: Any, source: Any, grant: Any) -> Optional[tuple[str, int, Any]]:
    """``(mode, manifest_revision, manifest)`` this run attaches with, or None (a plain card: no custody)."""
    if source is not None:
        manifest = grant.manifest().raw if isinstance(grant, ws.SourceSnapshot) and grant.manifest_value else None
        return source.mode, int(source.manifest_revision or 0), manifest
    if (grant is _NO_SNAPSHOT or not isinstance(grant, ws.SourceSnapshot) or grant.has_source
            or claimed.workspace_kind != "worktree" or not ws.settings().enabled):
        return None
    row = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (claimed.id,)).fetchone()
    created_on = row[0] if row else None
    if created_on and created_on != _me(conn):  # NULL: created by this gateway (a local, never-imported row)
        return None
    return "legacy_adopt", 0, None


def attach(conn: sqlite3.Connection, claimed: Any, workspace: str, source: Any,
           grant: Any) -> Optional[board_calls.AttachedRun]:
    """Join custody (v3 §6). None: nothing to attach (plain card, legacy fallback). Raises :class:`AttachRefused`
    when the run must not spawn (the grant is then released unused)."""
    if sync.board_mode(conn) != "primary" or board_calls.board_lacks_source():
        return None
    plan = _attach_plan(conn, claimed, source, grant)
    if plan is None:
        return None
    mode, revision, manifest = plan
    if mode == "resume" and isinstance(grant, ws.SourceSnapshot) and grant.manifest_value is None:
        from dataclasses import asdict
        from hermes_cli.kanban_source_registration import resume_premanifest
        try:
            return resume_premanifest(conn, claimed.id, grant.grant, asdict(grant.custody))
        except board_calls.SourceCallRefused as exc:
            raise AttachRefused(exc.code, exc.detail) from exc
        except sync.LeaseUnavailable as exc:
            raise AttachRefused("attach_unanswered", str(exc), uncertain=True) from exc
    run_uid = sync.run_uid(conn, claimed.current_run_id)
    if not run_uid:
        raise AttachRefused("no_run_uid", "the run has no portable identity")
    source_path = str(source.path) if source is not None else str(workspace)
    protocol = max((grant.custody.source_protocol if isinstance(grant, ws.SourceSnapshot) and grant.custody else 1),
                   (manifest or {}).get("version", 1))
    identity = None
    if protocol >= 2:
        if not manifest:
            raise AttachRefused("source_identity_refused", "a protocol-2 attach has no published manifest to bind")
        from hermes_cli.kanban_source_registration import source_identity, match_manifest
        identity = source_identity(source_path, manifest["source_kind"])
        match_manifest(identity, manifest)
    seen = observe(source_path, None)
    run = board_calls.AttachedRun(
        run_id=int(claimed.current_run_id), task_id=claimed.id, run_uid=run_uid, mode=mode,
        manifest_revision=revision, branch=(source.branch if source is not None else seen["branch"]),
        path=source_path, pre_head=seen["head"], manifest=manifest, state="pending",
        machine=_me(conn), source_protocol=protocol, source_identity=identity)
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_source_registration import _owned_elsewhere
    # Share initial registration's writer lock and predicate, including replay after a refusal.
    with write_txn(conn):
        _owned_elsewhere(conn, claimed.id, source_path)
        board_calls.record_run(conn, run, allow_nested=True)  # reserve before an uncertain RPC
    try:
        board_calls.attach(conn, claimed.id, run_uid, mode=mode, manifest_revision=revision, source_protocol=protocol)
    except board_calls.BoardLacksSource as exc:
        _forget(conn, run)
        if source is not None:
            raise AttachRefused("board_lacks_source", exc.detail) from exc
        return None  # a legacy card on an old Board runs exactly as before, with no custody
    except board_calls.SourceCallRefused as exc:
        definite = exc.code in board_calls.NOT_ATTACHED
        if not definite:  # M3: an earlier unanswered attach of this run may have applied; keep the evidence
            raise AttachRefused(exc.code, exc.detail, uncertain=True) from exc
        _forget(conn, run)
        if mode == "legacy_adopt" and exc.code == "legacy_adopt_refused":
            _log.info("workspace_source: %s not adopted (%s); it runs without custody", claimed.id, exc)
            return None
        raise AttachRefused(exc.code, exc.detail) from exc
    except sync.LeaseUnavailable as exc:
        raise AttachRefused("attach_unanswered", str(exc), uncertain=True) from exc
    board_calls.update_run(conn, run.run_id, state="attached")
    return board_calls.attached_run(conn, run.run_id)


def _forget(conn: sqlite3.Connection, run: board_calls.AttachedRun) -> None:
    """Drop this attempt's observation after a refusal proving nothing is attached (M3).

    Compare-and-delete: only the still-``pending`` row of this exact run_uid goes, so a concurrent
    settlement (another attempt that saw ``applied``) is never erased.
    """
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute("DELETE FROM kanban_source_runs WHERE run_id = ? AND run_uid = ? AND state = 'pending'",
                     (int(run.run_id), run.run_uid))


# --- release with a receipt (pre-empts _record_task_failure) ---------------------------------------------------------

def abort_receipt(run: board_calls.AttachedRun, *, not_started: bool) -> dict:
    """``unchanged`` + ``abort: pre_spawn`` when no process ever started and the worktree is exactly what attached
    (clean, on B, HEAD == manifest.commit, or == the pre-attach HEAD with no manifest); ``retained`` otherwise.
    Explicit ``initial`` registration is mid-run and can never qualify for a pre-spawn restore."""
    commit = (run.manifest or {}).get("commit") if run.manifest else None
    seen = observe(run.path, run.branch)
    expected = commit or run.pre_head
    same = (seen["clean"] and seen["head"] is not None and seen["head"] == expected
            and (run.branch is None or seen["branch"] == run.branch))
    if not_started and same and run.mode != "initial":
        return {"run_uid": run.run_uid, "state": "unchanged", "abort": "pre_spawn",
                "manifest_revision": run.manifest_revision, "commit": commit}
    reason = "post_spawn_failure" if not not_started else "pre_spawn_worktree_changed"
    return {"run_uid": run.run_uid, "state": "retained", "manifest_revision": run.manifest_revision,
            "commit": None, "reason": reason}


def release_with_receipt(conn: sqlite3.Connection, task_id: str, run_id: int, receipt: dict, *, outcome: str,
                         error: str, **policy: Any) -> str:
    """Release this run carrying ``receipt``: sync ``/leases release`` first, the outbox release as fallback. Either
    way a ``kanban_failure_commands`` row claims the run_uid FIRST, so a later ``_record_task_failure`` (whose
    ``queue`` is first-wins) adds no second release. Returns ``sync`` / ``outbox`` / ``taken`` (already queued)."""
    from hermes_cli import kanban_failure_release as release
    from hermes_cli.kanban_db_connect import write_txn
    uid = sync.run_uid(conn, run_id)
    if not uid:
        raise ValueError("a source release needs the run's portable identity")
    if release.queued(conn, run_id):
        return "taken"
    fields = {**policy, "outcome": outcome, "error": error[:500], "source_receipt": receipt}
    try:
        board_calls.release(conn, task_id, uid, **fields)
    except (board_calls.SourceCallRefused, sync.LeaseUnavailable) as exc:
        _log.info("workspace_source: sync release of %s failed (%s); queueing it", task_id, exc)
        with write_txn(conn):
            release.queue(conn, task_id, run_id, **{k: v for k, v in fields.items() if k != "error"},
                          error=error)
        return "outbox"
    with write_txn(conn):
        release.mark_sent(conn, uid, task_id)
    return "sync"


def abort_attached(conn: sqlite3.Connection, claimed: Any, run: board_calls.AttachedRun, *, not_started: bool,
                   pid: Optional[int], outcome: str, error: str, **policy: Any) -> dict:
    """The pre-spawn abort helper (v4 §2.2, v4.1 R2-a). Kills a worker that may exist, then releases with the
    receipt BEFORE the caller's ``_record_task_failure``."""
    if pid:
        from hermes_cli import kanban_db_dispatch as dispatch
        dispatch._terminate_reclaimed_worker(int(pid), claimed.claim_lock)
    receipt = abort_receipt(run, not_started=not_started and not pid)
    how = release_with_receipt(conn, claimed.id, run.run_id, receipt, outcome=outcome, error=error, **policy)
    board_calls.update_run(conn, run.run_id, state="aborted")
    _log.info("workspace_source: %s aborted before its worker ran (%s, %s release)", claimed.id, receipt["state"], how)
    return receipt


# --- the launch ----------------------------------------------------------------------------------------------------

def _source_env(source: Any) -> dict:
    return {k: v for k, v in (("HERMES_KANBAN_SOURCE_COMMIT", source.commit), ("HERMES_KANBAN_SOURCE_PATH", source.path),
                              ("HERMES_KANBAN_SOURCE_BRANCH", source.branch)) if v}


def _prepare(conn: sqlite3.Connection, claimed: Any, workspace: Any, branch: Optional[str], source: Any) -> None:
    from hermes_cli import kanban_db_dispatch as dispatch
    kbw = dispatch._kbw
    kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if source is not None:
        # The source branch is the manifest's/custody's, and ``branch_name`` is fenced on such a card (here by
        # ``fence`` and on the Board as forbidden_field). Writing it, even NULL -> branch, raised SourceFenced out of
        # the dispatcher tick after the claim and leaked the lease until it went stale (LIVE t_01a1d5b1c1798e2a).
        # The worker only needs it in memory.
        if not (claimed.branch_name or "").strip():
            claimed.branch_name = source.branch
        kbw._maybe_emit_scratch_tip(conn, claimed.id, "worktree")
        claimed.source_env = _source_env(source)
        return
    if claimed.workspace_kind == "worktree":
        kbw.set_branch_name(conn, claimed.id, branch or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)


def _release_unprepared(conn: sqlite3.Connection, claimed: Any, exc: Exception, result: Any) -> None:
    """Recording the workspace failed after the claim and before any attach: release the grant unused (custody is
    untouched, nothing ran) and count the failure, instead of raising out of the tick and leaving the lease to go
    stale."""
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_failure_release as release
    from hermes_cli.kanban_db_connect import write_txn
    error = f"workspace_source: workspace record refused ({type(exc).__name__}: {exc})"
    _log.warning("kanban dispatcher: %s not launched: %s", claimed.id, error)
    with write_txn(conn):
        release.queue(conn, claimed.id, claimed.current_run_id, outcome="released", unused_grant=True, error=error)
    dispatch._record_task_failure(conn, claimed.id, error, outcome="released", release_claim=True, end_run=True,
                                  infrastructure=True)
    result.respawn_guarded.append((claimed.id, f"workspace_prepare_failed:{type(exc).__name__}"))


def _attach_or_release(conn: sqlite3.Connection, claimed: Any, workspace: Any, source: Any, grant: Any,
                       result: Any) -> tuple[bool, Optional[board_calls.AttachedRun]]:
    try:
        run = attach(conn, claimed, str(workspace), source, grant)
    except AttachRefused as exc:
        from hermes_cli import kanban_db_dispatch as dispatch
        _log.info("workspace_source: %s not attached (%s); releasing the grant unused", claimed.id, exc)
        error = f"workspace_source: attach refused ({exc})"
        run = board_calls.attached_run(conn, claimed.current_run_id)
        if run is not None:  # the attach may have applied: the receipt lets the Board put custody back
            abort_attached(conn, claimed, run, not_started=True, pid=None, outcome="released", error=error,
                           unused_grant=True)
        else:
            from hermes_cli import kanban_failure_release as release
            from hermes_cli.kanban_db_connect import write_txn
            with write_txn(conn):
                release.queue(conn, claimed.id, claimed.current_run_id, outcome="released", unused_grant=True,
                              error=error)
        dispatch._record_task_failure(conn, claimed.id, error, outcome="released", release_claim=True,
                                      end_run=True, infrastructure=True)
        result.respawn_guarded.append((claimed.id, f"workspace_source_attach_refused:{exc.code}"))
        return False, None
    if source is not None:
        from hermes_cli.kanban_source_dispatch import materialized_event
        materialized_event(conn, claimed, grant if isinstance(grant, ws.SourceSnapshot) else None, source)
    return True, run


def launch(conn: sqlite3.Connection, claimed: Any, workspace: Any, branch: Optional[str], source: Any, *,
           lane: str, board: Optional[str], spawn_fn: Callable, failure_limit: int, result: Any,
           count_spawn: Callable[[str], None]) -> bool:
    """Record the workspace, attach (source/legacy-adopt cards), then spawn. True when a spawn slot was used."""
    from hermes_cli import kanban_db_dispatch as dispatch
    grant = _pop_grant(claimed.id)
    try:
        _prepare(conn, claimed, workspace, branch, source)
    except Exception as exc:  # health: allow BLE001 -- a claimed card must never leak its lease out of the tick
        _release_unprepared(conn, claimed, exc, result)
        return False
    ok, run = _attach_or_release(conn, claimed, workspace, source, grant, result)
    if not ok:
        return False
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    from hermes_cli.kanban_model_tier import apply_model_tier
    stage, pid = "pre_spawn", None
    try:  # Apply inside spawn accounting: an unavailable independent reviewer fails closed.
        apply_model_tier(conn, claimed, lane=lane)
        stage = "spawning"
        pid = dispatch._call_spawn_fn(spawn_fn, claimed, str(workspace), board)
        stage = "spawned"
        if pid:
            dispatch._set_worker_pid(conn, claimed.id, int(pid), run_id=claimed.current_run_id,
                                     claim_lock=claimed.claim_lock)
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        dispatch._kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on spawn would let a task that keeps
        # timing out loop forever. Cleared only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        count_spawn(claimed.assignee)
        return True
    except Exception as exc:  # health: allow BLE001 -- moved spawn-accounting boundary: every failure is recorded below
        from hermes_cli.kanban_review_model import admission_refusal
        # Admission failed before any worker ran; preserve its retry budget (a repeated review refusal: visible block).
        infrastructure, exhausted, extra = admission_refusal(conn, claimed.id, exc)
        if infrastructure:
            dispatch._kb._log.warning("kanban dispatcher: spawn of %s deferred without penalty: %s", claimed.id, exc)
        if run is not None:
            not_started = stage == "pre_spawn" or (stage == "spawning" and isinstance(exc, _not_started_types()))
            abort_attached(conn, claimed, run, not_started=not_started, pid=pid, outcome="spawn_failed",
                           error=str(exc), failure_limit=failure_limit, force_trip=exhausted,
                           infrastructure=infrastructure, block_kind="transient", extra=extra or {})
        if dispatch._record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
            infrastructure=infrastructure, force_trip=exhausted, event_payload_extra=extra,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def receipt_json(receipt: dict) -> str:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"))
