"""The last checkpoint a ready card recorded, for the ready-wait notices that prompt a chat to act. [daveey]

Defect kanban-3debc730ab (2026-11-12): t_01a1c59e0410b2fb merged PR #1148 at 08:15Z and recorded it only as the
checkpoint summary of its continued run ("LANDED: PR1148 MERGED ... as main 6948d8bc"); its worker then went on
hold and the card sat ready. The ``chat_wait_unclaimed`` notice at 09:11Z said only "ready 30 min with no worker
... re-place on a capable gateway", so the chat re-read the card's older "NEXT: on approval rerun ship" comment and
created a duplicate landing card for a merged PR. The notices (``kanban_parent_unclaimed``, ``kanban_chat_wait``)
and the ``kanban_show`` ready hint now carry that checkpoint.

:func:`fields` reads it (payload fields ``last_checkpoint`` / ``last_checkpoint_at``); :func:`sentence` renders
it. An old payload without the fields renders nothing.
"""
from __future__ import annotations

import logging
import time
from typing import Any

_log = logging.getLogger(__name__)

# A run that ended by parking: its summary is the checkpoint/reason the next run (or a chat) must read first.
PARK_OUTCOMES = frozenset({"continued", "blocked", "scheduled"})
LIMIT = 200


def fields(conn, task_id: str, *, diagnostic: dict[str, Any] | None = None) -> dict[str, Any]:
    """``{"last_checkpoint", "last_checkpoint_at"}`` when ``task_id``'s latest run (core-update requeues and
    unused Board grants skipped) ended by parking with a summary; ``{}`` otherwise. Advisory: a failed read
    drops the fields, never the notice."""
    diagnostic = {} if diagnostic is None else diagnostic
    diagnostic["reason"] = "lookup_error"
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues
        run = latest_run_skipping_requeues(conn, task_id)
        if run is None:
            diagnostic["reason"] = "no_run"
            return {}
        diagnostic.update(run_id=run["id"], outcome=run["outcome"], ended_at=run["ended_at"],
                          run_uid=run["uid"] if "uid" in run.keys() else None)
        if run["outcome"] not in PARK_OUTCOMES:
            diagnostic["reason"] = "nonpark"
            return {}
        if run["ended_at"] is None:
            diagnostic["reason"] = "unfinished"
            return {}
        text = kb._first_line(run["summary"], LIMIT)
        result = {"last_checkpoint": text, "last_checkpoint_at": int(kb._to_epoch(run["ended_at"]) or 0)} if text else {}
        diagnostic["reason"] = "included" if text else "empty_summary"
        return result
    except Exception:  # health: allow BLE001 -- the checkpoint is advisory; the notice must still go out
        _log.warning("last-checkpoint: lookup failed for %s", task_id, exc_info=True)
        return {}


def notice_fields(conn, task_id: str) -> dict[str, Any]:
    """Persist bounded lookup evidence alongside a notice, not a second lookup or another event.

    Boot revision is intentionally unknown for unbootstrapped callers: today's disk HEAD cannot
    establish what an old process loaded. Run IDs are local; the UID correlates replicated rows.
    """
    import os
    import socket
    from pathlib import Path
    from hermes_cli.git_fingerprint import process_boot_fingerprint
    from hermes_cli.kanban_sync_client import SyncUnavailable, load_identity

    fingerprint = process_boot_fingerprint(Path(__file__).resolve().parents[1])
    diagnostic: dict[str, Any] = {
        "emitter_pid": os.getpid(), "emitter_host": socket.gethostname(),
        "core_revision": fingerprint.rsplit(":", 1)[-1] if fingerprint else None,
        "emitter_gateway": None,
    }
    try:
        diagnostic["emitter_gateway"] = load_identity().gateway
    except (SyncUnavailable, OSError, ValueError):
        pass  # local-only boards have no enrollment; host/PID still identify the emitter
    result = fields(conn, task_id, diagnostic=diagnostic)
    return {**result, "checkpoint_diagnostic": diagnostic}


def sentence(payload: Any) -> str:
    """``Last checkpoint (HH:MMZ): "<text>". Read it before ...; resume or finish what it records. `` (trailing
    space), or "" when ``payload`` has no ``last_checkpoint``."""
    payload = payload if isinstance(payload, dict) else {}
    text = str(payload.get("last_checkpoint") or "").strip()[:LIMIT]
    if not text:
        return ""
    at = payload.get("last_checkpoint_at")
    stamp = f" ({time.strftime('%H:%MZ', time.gmtime(int(at)))})" if isinstance(at, (int, float)) and at > 0 else ""
    return (f"Last checkpoint{stamp}: \"{text}\". Read it before re-placing the card or creating a replacement; "
            "resume or finish what it records. ")
