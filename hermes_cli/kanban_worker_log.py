"""Per-task worker log rotation, split out of ``hermes_cli.kanban_db_dispatch``."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Optional

DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    from hermes_cli.kanban_db_dispatch import _positive_int
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        from hermes_cli.kanban_db_dispatch import _positive_int
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        rotated = _rotated_log_path(log_path, 1)
        log_path.rename(rotated)
        # A log written before worker logs were made private would carry its mode into the backup.
        rotated.chmod(rotated.stat().st_mode & 0o7770)
    except OSError:
        pass


def exit_summary_marker() -> str:
    """The CLI exit-summary header (``cli_session_mixin.show_exit_summary``), in the active language."""
    from agent.i18n import t
    return t("cli.session.exit_resume_hint")


def log_noise_prefixes() -> tuple[str, ...]:
    from agent.i18n import t
    return ("session_id:", "Query:", t("cli.chat.initializing_agent"))


def current_run_log(raw: str, run_id=None) -> str:
    """[daveey] Only the dead run's own part of the append-only per-task log.

    Every run ends with an exit trailer (``[kanban-worker-exit] rc=N``). The dead run's own trailer
    may already close the tail; any earlier trailer closes a PREVIOUS run. Without this cut, a run that
    printed nothing (a worker that died at start) was reported with the previous run's final message,
    so its crash notice read as the worker re-asking the earlier question."""
    import re
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    starts = list(re.finditer(r'^\[kanban-worker-start\] run_id=(\d+)[ \t]*$', raw, re.MULTILINE))
    if starts:
        start = starts[-1]
        if run_id is not None and start.group(1) != str(run_id):
            return ''
        return raw[start.end():]
    # No marker anywhere: the log predates the marker (a worker spawned before this version was
    # deployed), so fall back to the trailer / Session cut below. Readers with a run_id read the whole
    # file, so "no marker" is never a truncated tail.
    trailers = list(re.finditer(r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"\d+\s*$", raw.rstrip(), re.MULTILINE))
    body = raw.rstrip()
    if trailers and trailers[-1].end() == len(body):
        body, trailers = body[:trailers[-1].start()], trailers[:-1]
    body = body[trailers[-1].end():] if trailers else body
    # A managed client's run starts with its ``Session: <id>`` line. A client before the reply-flush
    # fix wrote its reply after its own trailer (stdout flushed at exit), so text between the previous
    # trailer and this run's Session line is the previous run's reply (t_01a188a461c0178c run 10258).
    starts = list(re.finditer(r"^Session: \S+[ \t]*$", body, re.MULTILINE))
    return body[starts[-1].start():] if starts else body


def crash_worker_output(conn, task_id, run_id, pid, claim_lock, *, board=None) -> str:
    """Select run-owned managed output, retaining the legacy log reader's patch seam."""
    output = managed_worker_output(conn, task_id, run_id, pid, claim_lock)
    if output is not None:
        return output
    from hermes_cli import kanban_db_dispatch as dispatch
    return dispatch._worker_final_output(task_id, board=board, run_id=run_id)


def managed_worker_output(conn, task_id, run_id, pid, claim_lock) -> Optional[str]:
    """Prefer an attributable receipt over the shared log, even if its output is empty.

    Old submitters can still flush into the task log after a new run's start marker.
    Read on the reclaim transaction's connection; opening another connection can deadlock.
    A bound managed run without a receipt has no attributable text: do not trust its
    shared log either. None is reserved for legacy/unbound callers' log fallback.
    """
    if conn is None or run_id is None:
        return None
    bound, output = _run_receipt_output(conn, task_id, run_id, pid, claim_lock)
    if output is not None:
        return output[-400:]
    return "" if bound else None


def _run_receipt_output(conn, task_id, run_id, pid, claim_lock):
    """``(bound, last_output)`` of the run's newest receipt under *claim_lock* from *pid* (the
    spawned submitter, ``tasks.worker_pid``) or the interpreter ``worker_bound`` recorded under
    that claim; ``last_output`` is None when there is no such receipt."""
    from hermes_cli import kanban_db as kb

    bound_row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='worker_bound' ORDER BY id DESC LIMIT 1", (task_id, run_id),
    ).fetchone()
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    modern, selected = modern_receipt(conn, task_id, run_id, claim_lock)
    if modern:
        output = selected[1].get('last_output') if selected else None
        return True, output if isinstance(output, str) else None
    bound = kb._json_dict(bound_row[0]) if bound_row else {}
    pids = {pid}
    if bound.get('claim_lock') == claim_lock and bound.get('pid') is not None:
        pids.add(bound['pid'])
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='worker_result' ORDER BY id DESC", (task_id, run_id),
    )
    for row in rows:
        result = kb._json_dict(row[0])
        if result.get('claim_lock') == claim_lock and result.get('pid') in pids:
            output = result.get('last_output')
            if isinstance(output, str):
                return bound_row is not None, output
    return bound_row is not None, None


def record_submitter_result(conn, params, pid, exit_code, last_output, extra=None) -> bool:
    """Book a managed submitter's own refusal/error exit as its run's ``worker_result``. [daveey]

    The submitter (``kanban_worker_client``) is the run's ``tasks.worker_pid``; when the owner refuses
    it (an owner that died: \"Kanban owner refused: Gateway inaccessible: service_identity_unverified\")
    no interpreter ever writes a receipt, and a bound run's shared task log is not trusted
    (``managed_worker_output``), so the refusal must be booked here or the owner-death classifier
    loses it. Fenced on the exact attempt: the task still runs THIS run under THIS claim with *pid*
    as its worker, and the run row agrees, so a late submitter of an older run writes nothing. An
    existing receipt for the run (the interpreter's own exit, e.g. a 75 quota wall) is kept: the
    submitter's echo of it must not supersede it. Caller holds ``write_txn``; returns whether it wrote.
    """
    from hermes_cli import kanban_db as kb

    task_id, run_id, claim_lock = params['task_id'], int(params['run_id']), params['claim_lock']
    task = conn.execute("SELECT status, current_run_id, claim_lock, worker_pid FROM tasks WHERE id=?",
                        (task_id,)).fetchone()
    if task is None or tuple(task) != ('running', run_id, claim_lock, pid):
        return False
    run = conn.execute("SELECT status, claim_lock, task_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
    if run is None or tuple(run) != ('running', claim_lock, task_id):
        return False
    if _run_receipt_output(conn, task_id, run_id, pid, claim_lock)[1] is not None:
        return False
    kb._append_event(conn, task_id, 'worker_result', {
        'pid': pid, 'claim_lock': claim_lock, 'exit_code': int(exit_code),
        'last_output': str(last_output)[-500:], **(extra or {}), 'submitter': True}, run_id=run_id)
    return True


def record_submitter_owner_restart(conn, params, pid, last_output) -> bool:
    """Book the submitter's own exit 76 (``OwnerRestartLost``) as its run's witnessed owner-restart result.

    The owner books a restart itself only when it stops cleanly (``record_restart_interruption``). An owner
    that wedged or died never does, and the submitter's exit line is in the shared task log only, which a
    bound run's diagnosis never reads, so the sweep booked the bound interpreter's receipt instead and
    charged the card (paintbot-tools run 207, 2026-11-13). Unlike ``record_submitter_result`` an existing
    receipt does not stop it: 76 is the submitter's own observation (the owner never settled the turn), not
    an echo of the interpreter's exit. Same fence: this run, this claim, *pid* is the task's worker. Written
    only with the full owner-restart witness (spawn identity + every prior result/binding), the shape the
    classifier accepts (``kanban_failed_result_identity``); an unwitnessed later result would veto reclaim.
    Caller holds ``write_txn``; returns whether it wrote. [daveey]
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE
    from hermes_cli.kanban_failed_result_identity import owner_restart_identity_fields

    task_id, run_id, claim_lock = params['task_id'], int(params['run_id']), params['claim_lock']
    task = kb.get_task(conn, task_id)
    if task is None or (task.status, task.current_run_id, task.claim_lock, task.worker_pid) != (
            'running', run_id, claim_lock, pid):
        return False
    run = conn.execute("SELECT status, claim_lock, task_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
    if run is None or tuple(run) != ('running', claim_lock, task_id):
        return False
    witness = owner_restart_identity_fields(conn, {'task_id': task_id, 'run_id': run_id}, task)
    if not witness:
        return False
    kb._append_event(conn, task_id, 'worker_result', {
        'pid': pid, 'claim_lock': claim_lock, 'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
        'last_output': str(last_output)[-500:], 'owner_restart': True, 'submitter': True, **witness},
        run_id=run_id)
    return True


def _worker_final_output(task_id: str, board: Optional[str] = None, *, run_id=None) -> str:
    """Best-effort read of a dead worker's last printed text, for the board diagnostic.

    A ``chat -q`` worker's stdout/stderr are redirected to its per-task log
    (``_default_spawn``), so when it exits without a terminal board call the
    reason is usually sitting there: the model's own explanation of why it could
    not comply (#88603), or the rendered provider error (#46593). The reap used to
    discard it in favour of a canned message on every retry. Trims the CLI exit
    summary, rule lines and the ``session_id:`` trailer; returns "" (never raises)
    on a missing/empty log.

    ``board`` must come from the dispatching tick: ambient current-board resolution
    is wrong for every board but the one the dispatcher thread happens to call
    "current", so the log would silently not be found.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_db_dispatch import _EXIT_TRAILER_RE, _LOG_CHROME, _worker_scope
    _exit_summary_marker, _log_noise_prefixes = exit_summary_marker, log_noise_prefixes
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=None if run_id is not None else 4000, board=board)
    except Exception:
        return ""
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", current_run_log(raw, run_id=run_id))
    cut = raw.rfind(_exit_summary_marker())
    if cut != -1:
        raw = raw[:cut]
    lines = []
    for ln in raw.splitlines():
        ln = _LOG_CHROME.sub("", ln).strip()
        if ln and not ln.startswith((*_log_noise_prefixes(), _worker_scope.SNAPSHOT_PREFIX)):
            lines.append(ln)
    return " ".join(lines)[-400:]



def open_worker_log(log_path: Path):
    """Rotate ``log_path``, then open it for binary append as the worker's stdout/stderr."""
    rotate_worker_log(log_path, *worker_log_rotation_config())
    # Worker output is durable and may quote secrets: never world-readable. Only the other bits
    # are stripped (a shared-group home keeps group access), and a log created before this fix
    # is tightened on its next open.
    # A private log owned by another account (shared-group home) is reopened as is: chmod would
    # raise EPERM. One that is still world-readable fails closed, with the descriptor released.
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o660)
    try:
        mode = os.fstat(fd).st_mode
        if mode & 0o007:
            os.chmod(log_path, mode & 0o7770)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "ab")
