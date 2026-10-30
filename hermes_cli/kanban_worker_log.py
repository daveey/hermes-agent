"""Per-task worker log rotation, split out of ``hermes_cli.kanban_db_dispatch``."""

from __future__ import annotations

import contextlib
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
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def exit_summary_marker() -> str:
    """The CLI exit-summary header (``cli_session_mixin.show_exit_summary``), in the active language."""
    from agent.i18n import t
    return t("cli.session.exit_resume_hint")


def log_noise_prefixes() -> tuple[str, ...]:
    from agent.i18n import t
    return ("session_id:", "Query:", t("cli.chat.initializing_agent"))


def current_run_log(raw: str) -> str:
    """[daveey] Only the dead run's own part of the append-only per-task log.

    Every run ends with an exit trailer (``[kanban-worker-exit] rc=N``). The dead run's own trailer
    may already close the tail; any earlier trailer closes a PREVIOUS run. Without this cut, a run that
    printed nothing (a worker that died at start) was reported with the previous run's final message,
    so its crash notice read as the worker re-asking the earlier question."""
    import re
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    trailers = list(re.finditer(r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"\d+\s*$", raw.rstrip(), re.MULTILINE))
    body = raw.rstrip()
    if trailers and trailers[-1].end() == len(body):
        body, trailers = body[:trailers[-1].start()], trailers[:-1]
    return body[trailers[-1].end():] if trailers else body
