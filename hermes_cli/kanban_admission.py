"""Kanban claim publication boundary for the POSIX updater fence, and the hub CPU/cap admission gate.

Hub CPU gate (worker-first overflow, docs/worker-first-v1.md): on a host whose installation-root config sets
``kanban.admission.cpu_busy_below_pct`` every new ready/review claim (legacy, host-bound and policy cards alike) must
pass, while ``admission.lock`` is held and before the claim commits: the advisory sampler's latest complete sample
(``cache/hub-cpu-sample.json``, at most ``ADVISORY_MAX_AGE_MS`` old) strictly below the limit, a whole-host running
count below the root ``kanban.max_in_progress`` cap, a lease deadline that is still ahead, and a hold inside its
budget. Any unknown refuses. No network and no sleep happen under the lock. A lock-free advisory read of the same
sample runs earlier, before any Board lease, and demands ``PRELEASE_HEADROOM_MS`` of extra freshness, so the sample it
accepts is still fresh when the locked gate reads it and a busy or stale hub causes no grant/release churn.

[daveey] The locked gate used to take its own ~1 s ``/proc/stat`` sample and count the host twice around it. In the
hub gateway (a process holding the GIL ~100 % of a core) the census alone ran ~1.3 s, so census + sample + recount
overran the 2.25 s hold on every claim, after the lease was granted, and was reported as ``hub_cpu_unknown`` while the
advisory was fresh (2026-11-13, t_01a1d2e5c4899f40). An overrun is now ``hub_admission_overrun``.
"""
import contextlib
import logging
import threading
import time
from pathlib import Path

ADVISORY_MAX_AGE_MS = 15_000  # the locked gate's freshness limit for the sampler's sample
# The pre-lease advisory wants this much more freshness, so the sample it accepts survives the Board lease round-trip
# and the txn wait inside ADVISORY_MAX_AGE_MS (the sampler rewrites it every 5 s: a healthy one is <= ~7 s old).
PRELEASE_HEADROOM_MS = 5_000
# The pre-lease advisory's freshness limit (10 s): an older sample defers every claim here. Shared with the
# stale-advisory alert (``kanban_hub_cpu_alert``), which must call a sample stale at the age the advisory defers.
PRELEASE_MAX_AGE_MS = ADVISORY_MAX_AGE_MS - PRELEASE_HEADROOM_MS
OVERRUN = "hub_admission_overrun"
_consumed = threading.local()
_decision = threading.local()  # the last cap count this thread's final gate read (refusal log / claimed event)


def _now() -> float:
    return time.time()


def _defer(task_id, reason):
    if task_id is None:
        return
    from hermes_cli import kanban_db_sync as sync
    sync.defer_claim(task_id, reason, sync.ORIGIN_LOCAL)  # this host's gate, never the Board


def hub_cpu_advisory(task_id=None, *, conn=None):
    """Lock-free pre-check before a Board lease (review ruling 5). Returns a refusal reason or None.

    Off unless the root limit is set. Uses the advisory sampler's latest complete sample: missing, stale (>10 s here),
    future or malformed (a non-number field, a busy % outside 0-100, a window outside 1-2 s) defers as
    ``hub_cpu_unknown``; a fresh value at or above the limit defers. An unreadable root limit defers as
    ``hub_cpu_limit_unknown``, unreadable/invalid priority-pool settings as ``hub_cpu_pool_limit_unknown``. The
    card's admission class is a plain read on ``conn``: a missing row uses the regular limit and a ``sqlite3.Error``
    propagates like the claim's other reads on that connection. Advisory only.
    Freshness here is ``PRELEASE_MAX_AGE_MS`` (``ADVISORY_MAX_AGE_MS - PRELEASE_HEADROOM_MS``, 10 s): the locked
    gate re-reads the same file with the looser ``ADVISORY_MAX_AGE_MS``, so a sample accepted before the lease is not
    stale by the time the gate reads it."""
    from hermes_cli import machine_cpu_metrics as cpu

    from hermes_cli.worker_resource_admission import refusal
    from hermes_cli.worker_compute_capacity import worker_enabled, owner_for_connection
    try:
        claimant = owner_for_connection(conn, task_id) if conn is not None and worker_enabled() else None
    except Exception:  # health: allow BLE001 -- malformed worker policy/identity must refuse prelease
        _defer(task_id, 'worker_resources_unknown')
        return 'worker_resources_unknown'
    if reason := refusal(claimant=claimant):
        _defer(task_id, reason)
        return reason
    state, limit = cpu.root_limit()
    if state == cpu.OFF:
        return None
    reason = "hub_cpu_unknown" if state == cpu.ON else HUB_CPU_LIMIT_UNKNOWN
    if state == cpu.ON:
        if conn is not None:
            from hermes_cli.kanban_admission_pools import advisory_limit
            try:
                limit = advisory_limit(conn, task_id, limit)
            except (OSError, ValueError, TypeError):
                # the priority-pool settings are unreadable/invalid: not a sample problem
                _defer(task_id, HUB_CPU_POOL_LIMIT_UNKNOWN)
                return HUB_CPU_POOL_LIMIT_UNKNOWN
        last = sampler_sample(PRELEASE_MAX_AGE_MS)
        if last is not None:
            if cpu.below(last["cpu_busy_pct"], limit):
                return None
            reason = "hub_cpu_at_or_above_limit"
    _defer(task_id, reason)
    return reason


def sampler_sample(max_age_ms=None):
    """The advisory sampler's latest complete sample when usable, else None (unknown).

    One reader for the pre-lease advisory and the locked gate: the triple ``machine_cpu_sampler`` writes, with every
    field a number, a 1-2 s window, a busy percentage in [0, 100] and an end stamp 0..``max_age_ms`` ago (a future
    stamp is unknown). ``max_age_ms`` None is the locked gate's ``ADVISORY_MAX_AGE_MS``."""
    from hermes_cli import machine_cpu_metrics as cpu
    from hermes_cli import machine_metrics

    last = machine_metrics.read_hub_cpu_sample() or {}
    pct, window, end = (last.get(k) for k in cpu.FIELDS)
    if not (type(pct) in (int, float) and type(window) in (int, float) and type(end) in (int, float)):
        return None
    if not 0 <= pct <= 100 or not cpu.MIN_WINDOW_MS <= window <= cpu.MAX_WINDOW_MS:
        return None
    max_age_ms = ADVISORY_MAX_AGE_MS if max_age_ms is None else max_age_ms
    return dict(zip(cpu.FIELDS, (pct, window, end))) if 0 <= _now() * 1000 - end <= max_age_ms else None


def _root_max_in_progress(*, root=None):
    """Root ``kanban.max_in_progress`` resolved like the dispatcher (unset → memory-derived default).

    ``(ok, cap)``: ok False when the root config is unreadable or the value is invalid (refuses)."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_cli.kanban_db_dispatch import resolve_max_in_progress

    try:
        doc = read_config_doc_readonly(Path(root if root is not None else get_default_hermes_root()) / "config.yaml")
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


def _cap_full(echo=None, *, conn=None, task_id=None):
    """Whole-host cap check. ``echo`` (:func:`kanban_grant_echo.own_grant_echo`, proven under this claim's write
    txn) excludes only that one row: this claim's own unused Board grant echoed before the txn (review F5)."""
    from hermes_cli import machine_metrics

    ok, cap = _root_max_in_progress()
    _decision.pool, _decision.pool_limit = "regular", None
    if conn is not None and ok:
        from hermes_cli.kanban_admission_pools import candidate_pool
        try:
            pool, ceiling, census = candidate_pool(conn, task_id, cap, echo=echo)
        except (OSError, ValueError, TypeError):
            return True
        if census is not None:
            _decision.counts = census
            _decision.pool = pool
            _decision.pool_limit = ceiling if pool == "priority" else None
            return pool is None
    detail = {}
    active = machine_metrics.active_card_runs(exclude=echo, detail=detail) if echo else machine_metrics.active_card_runs()
    _decision.counts = {"active_before": active + detail.get("excluded", 0) if type(active) is int else None,
                        "active_after": active, "cap": cap,
                        **({"excluded_run_uid": echo.run_uid} if detail.get("excluded") else {})}
    return not ok or type(active) is not int or active < 0 or (cap is not None and active >= cap)


COMMIT_RESERVE_S = 0.25  # lock time left for the SQL commit after the gate admits

# [daveey] One reason per final-gate refusal branch (hub 2026-11-13: the branches shared ``hub_cpu_unknown``, so the
# skip event and the queue line could not say which fired). The lock-free advisory keeps ``hub_cpu_unknown`` only for
# its own missing/stale/future/malformed sample and reports ``HUB_CPU_POOL_LIMIT_UNKNOWN`` when the pool limit it
# compares against cannot be read; a hold overrun is ``OVERRUN``.
HUB_CPU_LIMIT_UNKNOWN = "hub_cpu_limit_unknown"  # root kanban.admission.cpu_busy_below_pct unreadable/invalid
HUB_CPU_POOL_LIMIT_UNKNOWN = "hub_cpu_pool_limit_unknown"  # advisory: priority-pool settings unreadable/invalid
HUB_CPU_GATE_EXPIRED = "hub_cpu_gate_expired"  # the claim/lease deadline had passed before the census
HUB_CPU_SAMPLE_STALE = "hub_cpu_sample_stale"  # the sampler's sample is missing, stale or malformed at the lock
HUB_CPU_GATE_EXPIRED_COUNTING = "hub_cpu_gate_expired_counting"  # the deadline passed during the census
FINAL_GATE_REASONS = (HUB_CPU_LIMIT_UNKNOWN, HUB_CPU_GATE_EXPIRED, HUB_CPU_SAMPLE_STALE, HUB_CPU_GATE_EXPIRED_COUNTING,
                      OVERRUN)


def _hold_budget_s():
    from hermes_cli.admission_fence import CLAIM_MAX_HOLD_S
    return CLAIM_MAX_HOLD_S - COMMIT_RESERVE_S


def _over_budget(started, needed_s=0.0):
    return started is not None and time.monotonic() - started + needed_s > _hold_budget_s()


def _gate_refusal(reason, stage, started, **extra):
    """A final-gate refusal: its own reason code per branch (one shared ``hub_cpu_unknown`` hid which branch
    fired), plus evidence naming the stage and the hold so far, so the refusal log tells a hold overrun from an
    invalid sample, an expired lease or an unreadable limit (they all fail closed the same way)."""
    hold = {"hold_ms": round((time.monotonic() - started) * 1000), "budget_ms": round(_hold_budget_s() * 1000)} \
        if started is not None else {}
    return reason, {"stage": stage, **hold, **extra}


def _final_hub_gate(gate, started=None, claimant=None, *, conn=None, task_id=None):
    """Under admission.lock: ``(reason, evidence)``; reason None admits. Never touches the network or sleeps.

    One whole-host census (it picks the pool and its CPU ceiling), then the advisory sampler's sample re-read through
    :func:`sampler_sample` (at most ``ADVISORY_MAX_AGE_MS`` old, the same triple the pre-lease advisory accepted or a
    newer one), then the lease deadline and the hold. Nothing between the census and the commit waits, so it is not
    recounted. ``started`` (monotonic, taken when admission.lock was acquired) enforces the designed hold: a gate that
    overran ``CLAIM_MAX_HOLD_S`` minus the commit reserve (a slow board scan in a GIL-starved process, a stall) refuses
    as ``hub_admission_overrun`` instead of committing, so a waiting turn's BUSY_WAIT_S budget is never outlasted by an
    admitted claim (final review N1). The overrun is not a CPU reading and is never reported as one.

    Back-to-back local claims inside one sampler period (~5 s) read the same pre-claim sample, which cannot show the
    CPU the first claim's worker is about to use: between them only the ``max_in_progress`` census (it counts the
    committed run) protects the host. The gate's own independent re-sample was dropped deliberately: in the GIL-busy
    hub gateway census + ~1 s sample + recount overran the 2.25 s hold on every claim, after the lease was granted,
    and every claim deferred as ``hub_cpu_unknown`` (2026-11-13, t_01a1d2e5c4899f40)."""
    from hermes_cli import machine_cpu_metrics as cpu

    echo = getattr(gate, "echo", None)
    from hermes_cli.worker_resource_admission import refusal
    from hermes_cli.worker_compute_capacity import worker_enabled
    try:
        worker = worker_enabled()
    except Exception:  # health: allow BLE001 -- malformed worker policy refuses admission
        return "worker_resources_unknown", None
    if worker and not getattr(_decision, "worker_fenced", True):
        return "worker_resources_unknown", None
    if reason := refusal(echo, enabled=worker, claimant=claimant):
        return reason, None
    if worker and (_expired(gate) or _over_budget(started)):
        return "worker_resources_unknown", None
    state, limit = cpu.root_limit()
    if state == cpu.OFF:
        return None, None
    if state != cpu.ON:
        return _gate_refusal(HUB_CPU_LIMIT_UNKNOWN, "limit_unknown", started)
    if _expired(gate):
        # the claim/lease deadline already passed
        return _gate_refusal(HUB_CPU_GATE_EXPIRED, "lease_expired_before_census", started)
    if _cap_full(echo, conn=conn, task_id=task_id):
        return "hub_capacity_full", None
    limit = getattr(_decision, "pool_limit", None) or limit
    measured = sampler_sample()
    if measured is None:
        return _gate_refusal(HUB_CPU_SAMPLE_STALE, "sample_stale_or_missing", started)
    if not cpu.below(measured["cpu_busy_pct"], limit):
        return "hub_cpu_at_or_above_limit", None
    if _expired(gate):
        # the deadline passed while counting: never start
        return _gate_refusal(HUB_CPU_GATE_EXPIRED_COUNTING, "lease_expired_after_census", started)
    if _over_budget(started):
        return _gate_refusal(OVERRUN, "hold_after_census", started)  # refuse rather than outlast waiting turns
    counts = getattr(_decision, "counts", None) or {}
    admission = {k: counts[k] for k in ("active_before", "active_after", "excluded_run_uid")} \
        if counts.get("excluded_run_uid") else None
    if "pools" in counts:
        admission = {**(admission or {}), "pool": _decision.pool, "pools": counts["pools"]}
    return None, {**{k: measured[k] for k in cpu.FIELDS}, "limit_pct": limit,
                  **({"hub_admission": admission} if admission else {})}


def consumed_hub_cpu():
    """The sample the final gate admitted this claim with (for the ``claimed`` event), or None."""
    return getattr(_consumed, "evidence", None)


def claim_and_open_run(conn, task_id, source_status, lock, expires, now, *, event_extra=None, gate=None,
                       expect_assignee=None):
    """CAS to running and publish its run/event. Caller owns the fenced transaction. With
    ``expect_assignee`` the CAS also loses when the card was reassigned since the dispatch gates (#147534)."""
    from hermes_cli import kanban_db as kb

    # Activity belongs to this run, not the previous worker. Seed it atomically
    # so the first expired TTL cannot treat a freshly woken worker as wedged.
    # [daveey] Review F5: this claim's own grant may already have been echoed ``running`` (own holder, its run uid,
    # no lock) before the txn; ``gate.echo`` is that proof, re-checked here in the same txn, so the CAS adopts the
    # row instead of losing to it. Any other ``running`` row still loses.
    from hermes_cli.kanban_lane_workers import at_capacity
    if at_capacity(conn, task_id):
        return None
    echo = getattr(gate, "echo", None)
    if echo is not None:
        from hermes_cli.kanban_grant_echo import match_rows
        if match_rows(conn, echo) != [echo.run_id]:
            return None
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'running', human_request = NULL, claim_lock = ?,
               claim_expires = ?, started_at = COALESCE(started_at, ?),
               last_heartbeat_at = ?
         WHERE id = ? AND claim_lock IS NULL AND (status = ? OR (? AND status = 'running' AND current_run_id = ?))
           AND (? IS NULL OR assignee = ?)
        """,
        (lock, expires, now, now, task_id, source_status, echo is not None, echo.run_id if echo else None,
         expect_assignee, expect_assignee),
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
        "SELECT id FROM task_runs WHERE uid = ? AND task_id = ? AND (status = 'pending_sync' OR "
        "(id = ? AND status = 'running' AND claim_lock IS NULL AND ended_at IS NULL))",
        (run_uid, task_id, echo.run_id if echo and echo.run_uid == run_uid else None),
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
    from hermes_cli.kanban_admission_origin import origin_key
    conn.execute(
        "UPDATE task_runs SET admission_pool=?, admission_origin=?, "
        "admission_class=(SELECT admission_class FROM tasks WHERE id=?) WHERE id=?",
        (getattr(_decision, "pool", None) or "regular", origin_key(conn, task_id), task_id, run_id),
    )
    hub_cpu = dict(consumed_hub_cpu() or {})
    admission = hub_cpu.pop("hub_admission", None)
    kb._append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {}),
         **({"hub_cpu": hub_cpu} if hub_cpu else {}), **({"hub_admission": admission} if admission else {})},
        run_id=run_id,
    )
    return run_id


def disk_admission_refused(task_id=None):
    """Independent card-only disk check; never writes the updater/operator hold."""
    from hermes_cli.kanban_disk_admission import refusal_reason

    reason = refusal_reason()
    if reason:
        _defer(task_id, reason)
        logging.getLogger(__name__).info("Disk admission refused Kanban claim %s: %s", task_id, reason)
    return reason is not None


def admission_hold_set(task_id=None):
    """[daveey] Lock-free early read of the updater hold, for callers to check BEFORE side effects that
    precede ``claim_transaction`` (the Board-service lease). Advisory only: ``claim_transaction`` still
    re-checks under admission.lock, so a hold set after this read is still honoured. An unreadable
    fence reads as "not set" here and is decided by that authoritative check."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import is_held, SUPPORTED

    if disk_admission_refused(task_id):
        return True
    if not SUPPORTED:
        return False
    try:
        held = is_held(Path(get_default_hermes_root()) / "core-update")
        if held:
            _defer(task_id, "update_admission_hold")
        return held
    except OSError:
        return False


def _bind_echo(conn, task_id, gate):
    """Under the claim's write txn: attach the proven echo of the gate's own unused grant (or None) to the gate."""
    if gate is None or task_id is None:
        return
    from hermes_cli.kanban_grant_echo import own_grant_echo
    gate.echo = own_grant_echo(conn, task_id, gate)


@contextlib.contextmanager
def claim_transaction(conn, *, task_id=None, gate=None):
    """Worker-drain admission (a latched, sealed or unknown node defers the claim, CPU policy on or
    off), then the real claim transaction. The lease spans COMMIT so the census sees a claim in flight."""
    from hermes_cli import worker_drain_fence as drain_fence
    try:
        lease = drain_fence.admit("kanban.claim")
    except drain_fence.DrainRefused as exc:
        _defer(task_id, 'worker_draining')
        logging.getLogger(__name__).warning("kanban claim deferred: %s", exc)
        yield False
        return
    try:
        with _claim_transaction(conn, task_id=task_id, gate=gate) as admitted:
            yield admitted
    finally:
        if lease is not None:
            lease.close()


@contextlib.contextmanager
def _claim_transaction(conn, *, task_id=None, gate=None):
    """Fence check, the hub CPU/cap gate and real SQL commit share admission.lock (never a savepoint).

    Board storage may be overridden independently of the installation. Use the
    process's installation root, not the board path. Hooks run after both locks.
    """
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import AdmissionBusy, admission_lock, is_held, turns_waiting, HOLD_MESSAGE, SUPPORTED
    from hermes_cli.kanban_db import write_txn

    if conn.in_transaction:
        raise RuntimeError("Kanban claim cannot run inside an existing transaction")
    _decision.pool = "regular"
    if not SUPPORTED:
        with write_txn(conn):
            if disk_admission_refused(task_id):
                yield False
                return
            _bind_echo(conn, task_id, gate)
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
                from hermes_cli import worker_compute_capacity as capacity
                installation = Path(get_default_hermes_root()).resolve()
                started = time.monotonic()
                # Account policy must serialize different HERMES_HOME installs.
                # Both locks are nonblocking while the DB writer is held; the
                # stack retains them THROUGH commit/rollback. Preserve each
                # installation's updater fence and avoid taking one lock twice.
                roots = [installation]
                worker = capacity.worker_enabled()
                from hermes_cli import worker_board_retirement as retirement
                pending_retirement = retirement.pending(conn)
                if worker or pending_retirement is not None:
                    account = capacity.policy_root().resolve()
                    if account != installation:
                        roots.insert(0, account)
                fences = [stack.enter_context(admission_lock(home, timeout=0)) for home in roots]
                held = any(is_held(root) for root in fences)
                waiting = not held and any(turns_waiting(root) for root in fences)
                if not held and not waiting and pending_retirement is not None:
                    retirement.discard(pending_retirement)
                if not held and not waiting and worker:
                    from hermes_cli.worker_account_boards import register
                    register(conn)
            except AdmissionBusy:
                # [daveey] Designed contention: another claimer (or hermes-sync) holds admission.lock
                # through its commit. A normal defer, not a failure: no WARNING/traceback, which the
                # coach signal detector would file as a defect.
                logging.getLogger(__name__).info("Update admission lock busy; deferring Kanban claim to the next tick")
                _defer(task_id, 'update_admission_busy')
                yield False
                return
            except Exception:  # health: allow BLE001 -- malformed account policy/inventory must fail closed
                logging.getLogger(__name__).warning("Update admission unavailable; deferring Kanban claim", exc_info=True)
                _defer(task_id, 'update_admission_unavailable')
                yield False
                return
            if held:
                logging.getLogger(__name__).info(HOLD_MESSAGE)
                _defer(task_id, 'update_admission_hold')
                yield False
                return
            if waiting:
                # [daveey] A chat/notification/review turn is waiting out this lock: claims retry next tick, turns
                # cannot, so the claim yields before the locked gate's census (review F1).
                logging.getLogger(__name__).info("A turn is waiting for update admission; deferring Kanban claim")
                _defer(task_id, 'turn_admission_waiting')
                yield False
                return
            # The CPU/cap gate runs HERE, with admission.lock held, and the claim commits before the lock is released,
            # so a concurrent claimer sees this run in its census. It re-reads the sampler's sample; it never sleeps.
            _bind_echo(conn, task_id, gate)
            _decision.counts = None
            _decision.pool = "regular"
            previous_fenced = getattr(_decision, "worker_fenced", True)
            _decision.worker_fenced = worker
            try:
                claimant = capacity.owner_for_connection(conn, task_id) if worker and task_id is not None else None
                reason, evidence = _final_hub_gate(gate, started, claimant, conn=conn, task_id=task_id)
            finally:
                _decision.worker_fenced = previous_fenced
            if reason is not None:
                # ``evidence`` on a final-gate refusal names its stage and the hold so far, never a sample.
                logging.getLogger(__name__).info("Hub admission refused Kanban claim %s: %s %s %s", task_id, reason,
                                                 getattr(_decision, "counts", None) or {}, evidence or {})
                _defer(task_id, reason)
                yield False
                return
            if disk_admission_refused(task_id):
                yield False
                return
            _consumed.evidence = evidence
            try:
                yield True
            finally:
                _consumed.evidence = None
