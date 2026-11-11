"""Kanban claim publication boundary for the POSIX updater fence, and the hub CPU/cap admission gate.

Hub CPU gate (worker-first overflow, docs/worker-first-v1.md): on a host whose installation-root config sets
``kanban.admission.cpu_busy_below_pct`` every new ready/review claim (legacy, host-bound and policy cards alike) must
pass, while ``admission.lock`` is held and before the claim commits: an independent full ~1 s CPU busy sample strictly
below the limit, a whole-host running count below the root ``kanban.max_in_progress`` cap, and a lease deadline that
is still ahead after sampling. Any unknown refuses. No network happens under the lock. A lock-free advisory read of the
reporter's latest sample runs earlier, before any Board lease, so a busy hub causes no grant/release churn.
"""
import contextlib
import logging
import threading
import time
from pathlib import Path

ADVISORY_MAX_AGE_MS = 15_000
_consumed = threading.local()


def _now() -> float:
    return time.time()


def _defer(task_id, reason):
    if task_id is None:
        return
    from hermes_cli import kanban_db_sync as sync
    sync._CLAIM_DEFERRALS[task_id] = reason


def hub_cpu_advisory(task_id=None):
    """Lock-free pre-check before a Board lease (review ruling 5). Returns a refusal reason or None.

    Off unless the root limit is set. Uses the metrics reporter's latest complete sample: missing, stale (>15 s),
    future or malformed reads as unknown and defers; a fresh value at or above the limit defers. Advisory only."""
    from hermes_cli import machine_cpu_metrics as cpu
    from hermes_cli import machine_metrics

    state, limit = cpu.root_limit()
    if state == cpu.OFF:
        return None
    reason = "hub_cpu_unknown"
    if state == cpu.ON:
        last = machine_metrics.read_hub_cpu_sample() or {}
        end = last.get("cpu_busy_sample_end_ms")
        age = _now() * 1000 - end if type(end) in (int, float) else None
        if age is not None and 0 <= age <= ADVISORY_MAX_AGE_MS:
            pct = last.get("cpu_busy_pct")
            if cpu.below(pct, limit):
                return None
            if type(pct) in (int, float) and 0 <= pct <= 100:
                reason = "hub_cpu_at_or_above_limit"
    _defer(task_id, reason)
    return reason


def _root_max_in_progress():
    """Root ``kanban.max_in_progress`` resolved like the dispatcher (unset → memory-derived default).

    ``(ok, cap)``: ok False when the root config is unreadable or the value is invalid (refuses)."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_cli.kanban_db_dispatch import resolve_max_in_progress

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / "config.yaml")
    except FileNotFoundError:
        doc = None
    except Exception:  # health: allow BLE001 -- unreadable root config refuses the claim
        return False, None
    raw = ((doc or {}).get("kanban") or {}).get("max_in_progress") if isinstance(doc, dict) else None
    if raw is not None and (type(raw) is not int or raw < 1):
        return False, None
    return True, resolve_max_in_progress(raw)


def _expired(gate):
    return gate is not None and getattr(gate, "expires", None) is not None and gate.expires <= int(_now())


def _cap_full():
    from hermes_cli import machine_metrics

    ok, cap = _root_max_in_progress()
    active = machine_metrics.active_card_runs()
    return not ok or type(active) is not int or active < 0 or (cap is not None and active >= cap)


COMMIT_RESERVE_S = 0.25  # lock time left for the SQL commit after the gate admits


def _hold_budget_s():
    from hermes_cli.admission_fence import CLAIM_MAX_HOLD_S
    return CLAIM_MAX_HOLD_S - COMMIT_RESERVE_S


def _over_budget(started, needed_s=0.0):
    return started is not None and time.monotonic() - started + needed_s > _hold_budget_s()


def _final_hub_gate(gate, started=None):
    """Under admission.lock: ``(reason, evidence)``; reason None admits. Never touches the network.

    Cheap checks (whole-host cap, lease deadline) run BEFORE the ~1 s sample so a claim that must be refused anyway
    never holds the lock for a sample (review F6); the deadline and the whole-host count are checked again after
    sampling, immediately before the commit. ``started`` (monotonic, taken when admission.lock was acquired) enforces
    the designed hold: a gate that cannot fit its sample inside ``CLAIM_MAX_HOLD_S`` minus the commit
    reserve refuses before sampling when not even a nominal 1 s sample fits, and one that overran it (slow board scan, stall) refuses instead of committing, so
    a waiting turn's BUSY_WAIT_S budget is never outlasted by an admitted claim (final review N1)."""
    from hermes_cli import machine_cpu_metrics as cpu

    state, limit = cpu.root_limit()
    if state == cpu.OFF:
        return None, None
    if state != cpu.ON:
        return "hub_cpu_unknown", None
    if _cap_full():
        return "hub_capacity_full", None
    if _expired(gate):
        return "hub_cpu_unknown", None
    if _over_budget(started, cpu.WINDOW_S):
        return "hub_cpu_unknown", None  # no room left for even a nominal sample inside the designed hold
    measured = cpu.sample()
    if not measured or not all(k in measured for k in cpu.FIELDS):
        return "hub_cpu_unknown", None
    if not cpu.MIN_WINDOW_MS <= measured["cpu_busy_window_ms"] <= cpu.MAX_WINDOW_MS:
        return "hub_cpu_unknown", None
    if not cpu.below(measured["cpu_busy_pct"], limit):
        return "hub_cpu_at_or_above_limit", None
    if _expired(gate):
        return "hub_cpu_unknown", None  # the claim/lease deadline passed while sampling: never start
    if _cap_full():  # the spec's recount immediately before commit
        return "hub_capacity_full", None
    if _over_budget(started):
        return "hub_cpu_unknown", None  # the hold overran its budget: refuse rather than outlast waiting turns
    return None, {**{k: measured[k] for k in cpu.FIELDS}, "limit_pct": limit}


def consumed_hub_cpu():
    """The sample the final gate admitted this claim with (for the ``claimed`` event), or None."""
    return getattr(_consumed, "evidence", None)


def claim_and_open_run(conn, task_id, source_status, lock, expires, now, *, event_extra=None, gate=None):
    """CAS to running and publish its run/event. Caller owns the fenced transaction."""
    from hermes_cli import kanban_db as kb

    # Activity belongs to this run, not the previous worker. Seed it atomically
    # so the first expired TTL cannot treat a freshly woken worker as wedged.
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'running', human_request = NULL, claim_lock = ?,
               claim_expires = ?, started_at = COALESCE(started_at, ?),
               last_heartbeat_at = ?
         WHERE id = ? AND status = ? AND claim_lock IS NULL
        """,
        (lock, expires, now, now, task_id, source_status),
    )
    if cur.rowcount != 1:
        return None
    run_uid = kb._sync.stage_claim(conn, task_id, gate)
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    fields = (trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
              lock, expires, trow["max_runtime_seconds"] if trow else None, now)
    # The lease's run uid goes into the row itself, so the run is the one the Board granted even when no
    # capture trigger is installed (see ``stage_claim``). A Board echo of this claim pulled before this txn left
    # a ``pending_sync`` placeholder with that uid: that row becomes the run, never a second one. [daveey]
    placeholder = conn.execute(
        "SELECT id FROM task_runs WHERE uid = ? AND task_id = ? AND status = 'pending_sync'", (run_uid, task_id),
    ).fetchone() if run_uid else None
    if placeholder is not None:
        run_id = placeholder[0]
        conn.execute(
            "UPDATE task_runs SET profile = ?, step_key = ?, status = 'running', claim_lock = ?, claim_expires = ?, "
            "max_runtime_seconds = ?, started_at = ? WHERE id = ?", (*fields, run_id),
        )
    else:
        uid_col, uid_val = (", uid", (run_uid,)) if run_uid else ("", ())
        run_cur = conn.execute(
            f"""
            INSERT INTO task_runs (
                task_id, profile, step_key, status, claim_lock, claim_expires, max_runtime_seconds, started_at{uid_col}
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?{', ?' if run_uid else ''})
            """,
            (task_id, *fields, *uid_val),
        )
        run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    hub_cpu = consumed_hub_cpu()
    kb._append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {}),
         **({"hub_cpu": hub_cpu} if hub_cpu else {})}, run_id=run_id,
    )
    return run_id


def admission_hold_set(task_id=None):
    """[daveey] Lock-free early read of the updater hold, for callers to check BEFORE side effects that
    precede ``claim_transaction`` (the Board-service lease). Advisory only: ``claim_transaction`` still
    re-checks under admission.lock, so a hold set after this read is still honoured. An unreadable
    fence reads as "not set" here and is decided by that authoritative check."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import is_held, SUPPORTED

    if not SUPPORTED:
        return False
    try:
        held = is_held(Path(get_default_hermes_root()) / "core-update")
        if held:
            _defer(task_id, "update_admission_hold")
        return held
    except OSError:
        return False


@contextlib.contextmanager
def claim_transaction(conn, *, task_id=None, gate=None):
    """Fence check, the hub CPU/cap gate and real SQL commit share admission.lock (never a savepoint).

    Board storage may be overridden independently of the installation. Use the
    process's installation root, not the board path. Hooks run after both locks.
    """
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import AdmissionBusy, admission_lock, is_held, turns_waiting, HOLD_MESSAGE, SUPPORTED
    from hermes_cli.kanban_db import write_txn

    if conn.in_transaction:
        raise RuntimeError("Kanban claim cannot run inside an existing transaction")
    if not SUPPORTED:
        with write_txn(conn):
            yield True
        return
    # BEGIN IMMEDIATE can wait through SQLite's busy timeout and retries. Do
    # that before taking the installation-wide flock, so a busy board cannot
    # stall updater holds or unrelated reservations. Never wait for admission
    # while owning the DB writer lock: an unavailable flock defers this claim.
    # The stack deliberately OUTLIVES write_txn: accepted claims must COMMIT
    # (including retries), or roll back on failure, before releasing admission.
    with contextlib.ExitStack() as stack:
        with write_txn(conn):
            try:
                root = stack.enter_context(admission_lock(get_default_hermes_root(), timeout=0))
                started = time.monotonic()
                held = is_held(root)
                waiting = not held and turns_waiting(root)
            except AdmissionBusy:
                # [daveey] Designed contention: another claimer (or hermes-sync) holds admission.lock
                # through its commit. A normal defer, not a failure: no WARNING/traceback, which the
                # coach signal detector would file as a defect.
                logging.getLogger(__name__).info("Update admission lock busy; deferring Kanban claim to the next tick")
                yield False
                return
            except (OSError, TimeoutError):
                logging.getLogger(__name__).warning("Update admission unavailable; deferring Kanban claim", exc_info=True)
                yield False
                return
            if held:
                logging.getLogger(__name__).info(HOLD_MESSAGE)
                yield False
                return
            if waiting:
                # [daveey] A chat/notification/review turn is waiting out this lock: claims retry next tick, turns
                # cannot, so the claim yields before its ~1 s sample (review F1).
                logging.getLogger(__name__).info("A turn is waiting for update admission; deferring Kanban claim")
                yield False
                return
            # Frozen contract: an independent full CPU sample is taken HERE, with admission.lock held, and the
            # claim commits before the lock is released, so a concurrent claimer sees this run in its recount.
            reason, evidence = _final_hub_gate(gate, started)
            if reason is not None:
                logging.getLogger(__name__).info("Hub admission refused Kanban claim %s: %s", task_id, reason)
                _defer(task_id, reason)
                yield False
                return
            _consumed.evidence = evidence
            try:
                yield True
            finally:
                _consumed.evidence = None
