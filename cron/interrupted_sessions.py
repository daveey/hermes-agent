"""End the state.db session rows of cron executions that were interrupted mid-turn.

A cron run's session row is titled and ended by the scheduler's ``_finalize_cron_session`` after the
agent turn returns. When the owning process dies mid-turn (gateway restart, core update, OOM), that
step never runs: ``recover_interrupted_executions`` marks the ledger row ``unknown`` in the next
process, but nothing closed the session row, so it stayed untitled and ``ended_at IS NULL`` forever
(it looks like a live run in every session list).

The ledger does not store the session id. The scheduler names the session
``cron_<job_id>_<YYYYmmdd_HHMMSS>`` right after it claims the execution, so the session of an
interrupted attempt is the unended ``cron_<job_id>_*`` row that started at/after the attempt's claim
and before the next claim of the same job (or the recovery time). The upper bound keeps a newer live
run of the same job out of the match.

Everything here is best-effort: bounded by a timeout, never raises, logs once per failure.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

END_REASON = "cron_interrupted"
# The session row is created a few ms after the claim; allow clock/rounding skew.
_CLAIM_SKEW_SECONDS = 5.0
# What a ledger/state.db/jobs.json read or write here can raise.
_STORAGE_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError)
DEFAULT_TIMEOUT_SECONDS = 15.0


def _ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _job_title_base(job_id: str) -> str:
    try:
        from cron.jobs import get_job

        job = get_job(job_id) or {}
        name = str(job.get("name") or "").strip()
    except _STORAGE_ERRORS as exc:  # jobs.json unreadable: the id-based title still works
        logger.debug("cron interrupted-session: job %s lookup failed: %s", job_id, exc)
        name = ""
    return " ".join(name.split())[:60].strip() or f"cron {job_id}"


def _fmt_when(started_at: float) -> str:
    try:
        from hermes_time import now as _hermes_now

        tz = _hermes_now().tzinfo
        return datetime.fromtimestamp(float(started_at), tz=tz).strftime("%b %d %H:%M")
    except (ImportError, ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(float(started_at)).strftime("%b %d %H:%M")


def _next_claims(job_ids: set[str]) -> dict[str, list[float]]:
    """Every claim time per job, from the ledger (sorted)."""
    from cron import executions

    out: dict[str, list[float]] = {j: [] for j in job_ids}
    if not job_ids:
        return out
    with executions._transaction() as conn:
        marks = ",".join("?" for _ in job_ids)
        for row in conn.execute(
            f"SELECT job_id, claimed_at FROM executions WHERE job_id IN ({marks})", tuple(job_ids)
        ):
            ts = _ts(row["claimed_at"])
            if ts is not None:
                out[row["job_id"]].append(ts)
    for v in out.values():
        v.sort()
    return out


def _candidate_sessions(db, record: dict[str, Any], claims: list[float]) -> list[dict[str, Any]]:
    job_id = str(record.get("job_id") or "")
    claimed = _ts(record.get("claimed_at"))
    if not job_id or claimed is None:
        return []
    upper = _ts(record.get("finished_at"))
    later = [c for c in claims if c > claimed]
    if later:
        upper = min(later[0], upper) if upper is not None else later[0]
    escaped = job_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    sql = (
        "SELECT id, started_at, title FROM sessions "
        "WHERE id LIKE ? ESCAPE '\\' AND ended_at IS NULL AND started_at >= ?"
    )
    params: list[Any] = [f"cron\\_{escaped}\\_%", claimed - _CLAIM_SKEW_SECONDS]
    if upper is not None:
        sql += " AND started_at < ?"
        params.append(upper)
    return [dict(r) for r in db._read_all(sql + " ORDER BY started_at", params)]


def _title_interrupted(db, session_id: str, job_id: str, started_at: float) -> None:
    base = f"{_job_title_base(job_id)} · {_fmt_when(started_at)} (interrupted)"
    for title in (base, f"{base} {session_id[-6:]}"):
        try:
            if db.set_session_title(session_id, title):
                return
        except ValueError:  # title already used by another row: try the id-stamped one
            continue


def _end_one(db, sid: str) -> int:
    ended = 0
    targets = [sid]
    try:
        tip = db.get_compression_tip(sid)
        if tip and tip != sid:
            targets.append(tip)
    except _STORAGE_ERRORS as exc:
        logger.debug("cron interrupted-session: compression tip of %s failed: %s", sid, exc)
    for target in targets:
        row = db.get_session(target)
        if row is None or row.get("ended_at") is not None:
            continue
        db.end_session(target, END_REASON)
        ended += 1
    return ended


def _end_sessions_body(records: list[dict[str, Any]], db_path: Optional[Path]) -> int:
    from hermes_state import SessionDB

    claims = _next_claims({str(r.get("job_id")) for r in records if r.get("job_id")})
    db = SessionDB(db_path=db_path)
    ended = 0
    try:
        for record in records:
            job_id = str(record.get("job_id") or "")
            try:
                for sess in _candidate_sessions(db, record, claims.get(job_id, [])):
                    sid = sess["id"]
                    if not sess.get("title"):
                        _title_interrupted(db, sid, job_id, float(sess["started_at"]))
                    n = _end_one(db, sid)
                    if n:
                        ended += n
                        logger.info("Job '%s': ended interrupted cron session %s (execution %s)",
                                    job_id, sid, record.get("id"))
            except _STORAGE_ERRORS as exc:
                logger.warning("Job '%s': could not end interrupted cron session for execution %s: %s",
                               job_id, record.get("id"), " ".join(str(exc).split()))
    finally:
        try:
            db.close()
        except (sqlite3.Error, OSError) as exc:
            logger.debug("cron interrupted-session: state.db close failed: %s", exc)
    return ended


def end_interrupted_cron_sessions(
    records: Iterable[dict[str, Any]], *, db_path: Optional[Path] = None,
    timeout_seconds: Optional[float] = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Title (if untitled) and end the session rows of interrupted executions *records*.

    Returns the number of session rows ended. Never raises; gives up after *timeout_seconds*
    (``None``/``<=0`` runs inline) so a locked state.db cannot stall the scheduler tick.
    """
    recs = [dict(r) for r in records if r]
    if not recs:
        return 0
    result: list[int] = []

    def _runner() -> None:
        try:
            result.append(_end_sessions_body(recs, db_path))
        except BaseException:  # health: allow BLE001 -- thread boundary: must never escape into the scheduler
            logger.warning("cron interrupted-session cleanup failed", exc_info=True)

    if not timeout_seconds or timeout_seconds <= 0:
        _runner()
        return result[0] if result else 0
    from agent.memory_provider import spawn_context_thread

    # Context-bound: a profile-scoped HERMES_HOME override must resolve the same state.db here.
    worker = spawn_context_thread(_runner, name="cron-interrupted-sessions")
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        logger.warning("cron interrupted-session cleanup exceeded %.1fs; rows may stay unended until "
                       "the next recovery", timeout_seconds)
        return 0
    return result[0] if result else 0


def backfill_interrupted_cron_sessions(
    *, days: float = 7.0, db_path: Optional[Path] = None,
    timeout_seconds: Optional[float] = None,
) -> int:
    """One-shot repair: end the sessions of every ``unknown`` (interrupted) execution in the last
    *days*. Safe to rerun; only unended rows are touched."""
    from cron import executions

    try:
        cutoff = executions._hermes_now().timestamp() - days * 86400
        with executions._transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM executions WHERE status='unknown'")]
    except _STORAGE_ERRORS as exc:
        logger.warning("cron interrupted-session backfill: ledger read failed: %s", exc)
        return 0
    rows = [r for r in rows if (_ts(r.get("claimed_at")) or 0) >= cutoff]
    ended = end_interrupted_cron_sessions(rows, db_path=db_path, timeout_seconds=timeout_seconds)
    return ended + end_orphaned_cron_sessions(days=days, db_path=db_path)


def _job_id_of(session_id: str) -> str:
    # cron_<job_id>_<YYYYmmdd>_<HHMMSS>
    return session_id[len("cron_"):].rsplit("_", 2)[0]


def end_orphaned_cron_sessions(
    *, days: float = 7.0, min_age_seconds: float = 3600.0, db_path: Optional[Path] = None,
) -> int:
    """End unended ``cron_*`` session rows that no live attempt can still own.

    Covers rows whose ledger attempt was already pruned, or settled without the owner finalizing
    the session. A row is left alone when it is younger than *min_age_seconds* or when its job has
    a ``claimed``/``running`` attempt claimed at or before the row started (that attempt may still
    be writing it). Manual/one-shot repair; never raises.
    """
    from cron import executions
    from hermes_state import SessionDB

    try:
        now = executions._hermes_now().timestamp()
        with executions._transaction() as conn:
            live = [dict(r) for r in conn.execute(
                "SELECT job_id, claimed_at FROM executions WHERE status IN ('claimed','running')")]
    except _STORAGE_ERRORS as exc:
        logger.warning("cron orphan-session sweep: ledger read failed: %s", exc)
        return 0
    live_claims: dict[str, list[float]] = {}
    for r in live:
        ts = _ts(r.get("claimed_at"))
        if ts is not None:
            live_claims.setdefault(str(r["job_id"]), []).append(ts)
    ended = 0
    try:
        db = SessionDB(db_path=db_path)
    except _STORAGE_ERRORS as exc:
        logger.warning("cron orphan-session sweep: state.db open failed: %s", exc)
        return 0
    try:
        rows = [dict(r) for r in db._read_all(
            "SELECT id, started_at, title FROM sessions WHERE id LIKE 'cron\\_%' ESCAPE '\\' "
            "AND ended_at IS NULL AND started_at >= ? AND started_at < ?",
            (now - days * 86400, now - min_age_seconds))]
        for row in rows:
            sid, started = row["id"], float(row["started_at"])
            job_id = _job_id_of(sid)
            if any(c - _CLAIM_SKEW_SECONDS <= started for c in live_claims.get(job_id, [])):
                continue
            try:
                if not row.get("title"):
                    _title_interrupted(db, sid, job_id, started)
                n = _end_one(db, sid)
            except _STORAGE_ERRORS as exc:
                logger.warning("cron orphan-session sweep: %s: %s", sid, exc)
                continue
            if n:
                ended += n
                logger.info("Job '%s': ended orphaned cron session %s", job_id, sid)
    except _STORAGE_ERRORS as exc:
        logger.warning("cron orphan-session sweep failed: %s", exc)
    finally:
        try:
            db.close()
        except (sqlite3.Error, OSError) as exc:
            logger.debug("cron orphan-session sweep: state.db close failed: %s", exc)
    return ended
