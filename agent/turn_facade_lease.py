"""Durable cross-process session turn lease for ``TurnFacadeMixin.run_conversation``.

One process at a time may load -> run -> flush a session shared through state.db (Desktop, CLI
resume, gateway, background delivery). ``admit_durable_turn_lease`` acquires the row lease (or
returns the early result the façade must hand back); ``DurableTurnLease`` owns the periodic
refresher, the turn-liveness watchdog wiring, and the lease-loss / stall interrupt plumbing. Both
timers run via the shared scheduler (``agent/periodic_scheduler.py``; timer thread orders,
bodies run on per-handle workers), not per-turn threads.
"""
import logging
import os
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from hermes_state_pidns import holder_namespace_token

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("run_agent")

# ``tool_reason`` for a lost session turn lease: attributes the stop to the lease, not the user (#112647).
_REASON_LEASE_LOST = "session turn lease lost"

LEASE_TTL_SECONDS = 300.0
LEASE_WAIT_SECONDS = 1800.0
BUSY_DB_WAIT_SECONDS = 15.0  # floor for riding out a state.db write-lock spike, even with no holder wait
# SessionDB's default write patience: a renewal waits this long for the write lock, never past the authority.
_REFRESH_WRITE_PATIENCE_S = 20.0
# Headroom before expiry for one BEGIN IMMEDIATE blocked in the writer's 1s busy handler, plus jitter.
_REFRESH_EXPIRY_MARGIN_S = 2.0


_RELEASE_RETRY_FIRST_DELAY_S = 0.25
_RELEASE_RETRY_MAX_DELAY_S = 10.0


def _retry_lease_release(db, session_id: str, holder: str, give_up_at: float) -> None:
    """Background retry of a lock-failed turn-lease DELETE, with backoff, until ``give_up_at``
    (wall clock). Logs one ERROR if every attempt failed; the store forgets the abandoned holder
    once a DELETE commits."""
    from hermes_state_errors import classify_persistence_error
    delay = _RELEASE_RETRY_FIRST_DELAY_S
    attempts = 0
    while True:
        remaining = give_up_at - time.time()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _RELEASE_RETRY_MAX_DELAY_S)
        remaining = give_up_at - time.time()
        if remaining <= 0:
            break
        attempts += 1
        try:
            db.release_session_turn_lease(
                session_id, holder, patience_s=min(_REFRESH_WRITE_PATIENCE_S, max(0.1, remaining)),
            )
        except Exception as exc:
            if classify_persistence_error(exc) == "locked":
                continue
            logger.error("Session turn lease release retry failed: %s", session_id, exc_info=True)
            return
        logger.info("Session turn lease released after %d deferred attempt(s): %s", attempts, session_id)
        return
    logger.error(
        "Session turn lease release retries exhausted after %d attempt(s); the row expires on its own: %s",
        attempts, session_id,
    )


def turn_admission_lock(agent):
    """Serialize turn admission with runtime publication. A switch refuses any active holder under
    this lock, so admission is either before the check (no swap) or after the publish (new runtime)."""
    return agent.__dict__.setdefault("_turn_admission_lock", threading.RLock())


class DurableTurnLease:
    """An admitted session turn lease plus the periodic timers that keep it alive and watch the turn.

    ``stop`` is shared by the refresher and the liveness watchdog; ``turn_active`` gates every
    interrupt so a late refresher miss can never hard-interrupt the NEXT turn. Both are read and
    written only under ``_lock``.
    """

    def __init__(self, agent, db, session_id: str, holder: str, *, expires_at: Optional[float] = None) -> None:
        self.agent = agent
        self.db = db
        self.session_id = session_id  # id at admission; release always targets this row
        self.holder = holder
        # Wall clock, same as the row's expires_at, never later than it: the row's committed expiry at
        # admission, then a timestamp taken BEFORE each successful renewal (the store stamps expiry
        # before its lock wait). Only a successful renewal moves it.
        self._authority_deadline = time.time() + LEASE_TTL_SECONDS if expires_at is None else expires_at
        self.stop = threading.Event()
        self.refresh_interval = float(getattr(agent, "_session_turn_lease_refresh_interval", 60.0))
        self._lock = threading.Lock()
        self.turn_active = False
        self.interrupt_message: Optional[str] = None
        self.watchdog = None  # TurnLivenessWatchdog when configured
        self.timer_handles: list = []  # periodic_scheduler handles, cancelled in join_threads

    def _current_session_id(self) -> str:
        return getattr(self.agent, "session_id", None) or self.session_id

    def build_threads(self) -> None:
        """Create (not schedule) the liveness watchdog when configured: lease renewal is NOT
        evidence of progress; a silently stalled turn would renew forever."""
        try:
            from hermes_cli.config import load_config_readonly

            liveness_config = load_config_readonly() or {}
        except Exception:
            liveness_config = {}
        from agent import turn_liveness

        timeout_s, poll_s = turn_liveness.resolve_turn_liveness_settings(liveness_config)
        if timeout_s is not None:
            self.watchdog = turn_liveness.TurnLivenessWatchdog(
                self.agent, session_id=self._current_session_id(), timeout_s=timeout_s,
                poll_s=poll_s, stop_event=self.stop,
                activity_lock=self.agent._liveness_activity_lock(),
                is_turn_active=self.is_turn_active, commit_abort=self.commit_liveness_abort,
                deactivate_turn=self.stop_refresher,
            )

    def start(self) -> None:
        with self._lock:
            self.turn_active = True
        # Stamp the activity clock at turn entry: `_last_activity_ts` persists across turns, so
        # without this the watchdog would measure idle from the PREVIOUS turn and abort a fresh one.
        self.agent._touch_activity("starting new turn")
        from hermes_cli.observability.shared_metrics_process import arm_turn
        arm_turn(self.agent)
        from agent.periodic_scheduler import schedule

        self.timer_handles.append(schedule(self.refresh_tick, self.refresh_interval))
        self.timer_handles.append(schedule(self.takeover_tick, 0.5))
        if self.watchdog is not None:
            self.timer_handles.append(self.watchdog.schedule())

    def takeover_tick(self):
        """Observe the existing registry handoff, qualified by this exact durable holder.

        Keep renewing until the turn unwinds and releases: an interrupt is NOT a release.
        Explicit DB home binding also works for multiplexed CLI/background turns.
        """
        from pathlib import Path
        from hermes_cli.active_sessions import find_takeover_of
        if self.stop.is_set():
            return False
        if find_takeover_of(self.holder, registry_home=Path(self.db.db_path).parent):
            self._interrupt_turn("This chat was taken over by another Hermes window.", taken_over=True)
            return False
        return None

    def stop_refresher(self) -> None:
        """Stop renewal and deactivate the turn. Also the watchdog's deactivate callback: a wedge the
        hard interrupt cannot unwind must not keep the lease alive forever; TTL expiry lets
        stale-turn cleanup reclaim the row."""
        with self._lock:
            self.turn_active = False
            self.stop.set()

    deactivate_after_liveness_abort = stop_refresher

    def join_threads(self, timeout: float = 1.0) -> None:
        """Cancel both timers; ``wait=timeout`` mirrors the old ``thread.join(timeout)`` so an
        in-flight tick finishes before ``clear_interrupt`` runs."""
        for handle in self.timer_handles:
            handle.cancel(wait=timeout)
        # The watchdog holds this lease's bound methods: drop it so the finished turn frees by refcount.
        self.timer_handles, self.watchdog = [], None

    def release(self) -> None:
        """Release the row and drop the agent's holder attrs (only if they still name this lease).

        A DELETE that loses the state.db write lock is not a failure yet: the store marks the holder
        abandoned (so this process's next turn can take the row) and a daemon thread retries the
        DELETE until the lease would have expired anyway, so other processes are not kept waiting."""
        self.stop_refresher()  # Revoke copied in-process custody even if DELETE is deferred.
        from hermes_state_errors import classify_persistence_error
        from agent.runtime_session_store import is_retired_generation_error
        agent = self.agent
        try:
            self.db.release_session_turn_lease(self.session_id, self.holder)
        except Exception as exc:
            if classify_persistence_error(exc) == "locked":
                logger.warning(
                    "Session turn lease release deferred (state.db busy); retrying in the background: %s (%s)",
                    self.session_id, exc,
                )
                self._start_release_retry()
            elif is_retired_generation_error(exc):
                # The owner retired this worker generation (restart/update) and drops its rows itself.
                logger.warning(
                    "Session turn lease release skipped: worker generation retired: %s", self.session_id,
                )
            else:
                logger.error("Failed to release session turn lease: %s", self.session_id, exc_info=True)
        if getattr(agent, "_active_session_turn_lease_holder", None) == self.holder:
            agent._active_session_turn_lease_holder = None
            agent._active_session_turn_lease_ttl_seconds = None

    def _start_release_retry(self) -> None:
        # Retry until the row would expire on its own (plus the expiry margin): past that point any
        # acquirer reclaims it, so further retries buy nothing.
        give_up_at = max(self._authority_deadline, time.time()) + _REFRESH_EXPIRY_MARGIN_S
        threading.Thread(
            target=_retry_lease_release, args=(self.db, self.session_id, self.holder, give_up_at),
            name=f"turn-lease-release-retry:{self.session_id}", daemon=True,
        ).start()

    def is_turn_active(self) -> bool:
        with self._lock:
            return self.turn_active

    def _interrupt_turn(self, message: str, *, taken_over: bool = False) -> None:
        """Lease-loss interrupts fire UNCONDITIONALLY (no generation claim): a lost lease means
        this process no longer owns the session. Only the watchdog's stalls can be spuriously stale."""
        with self._lock:
            if self.stop.is_set() or not self.turn_active:
                return
            if taken_over:
                # Survive finalizer interrupt clearing: a queued completion/goal must
                # not reacquire the row in the gap before the new window starts.
                self.agent._taken_over_session_id = self._current_session_id()
                self.agent._taken_over_db_path = self.db.db_path
            self.interrupt_message = message
            try:
                self.agent.interrupt(message, hard_cancel=True, tool_reason=_REASON_LEASE_LOST)
            except Exception:
                self.agent._interrupt_requested = True
                self.agent._interrupt_message = message
                self.agent._tool_interrupt_reason = _REASON_LEASE_LOST

    def commit_liveness_abort(self, snapshot, message: str) -> bool:
        """Commit point for the watchdog's stall observation.

        Revalidates the observed ``(generation, timestamp)`` under the SAME lock ``_touch_activity``
        uses, so a turn that resumed while the stall was logged is never hard-cancelled; the
        revalidated generation is consumed by ``interrupt(require_generation=...)`` with the first
        publication in ONE critical section. If ``interrupt`` raises, the abort declines FAIL-CLOSED.
        Returns False when stale or already winding down."""
        agent = self.agent
        with agent._liveness_activity_lock():
            current_generation = getattr(agent, "_turn_liveness_activity_generation", 0)
            if (current_generation, getattr(agent, "_last_activity_ts", None)) != (
                snapshot.generation, snapshot.activity_ts
            ):
                return False
        with self._lock:
            if self.stop.is_set() or not self.turn_active:
                return False
        try:
            published = agent.interrupt(
                message, hard_cancel=True, tool_reason="turn liveness watchdog",
                require_generation=current_generation,
            )
        except Exception:
            logger.debug("Turn liveness abort interrupt raised; declining the abort", exc_info=True)
            published = False
        if published is False:
            # Claim went stale between revalidation and the hammer: real progress landed.
            return False
        with self._lock:
            self.interrupt_message = message
        return True

    def clear_interrupt(self) -> None:
        """Clear only the interrupt admitted by this lease's refresher/watchdog. Run AFTER join."""
        message = self.interrupt_message
        if not message:
            return
        agent = self.agent
        from tools.interrupt import set_interrupt as _set_interrupt

        with getattr(agent, "_pending_redirect_lock", None) or nullcontext():
            if getattr(agent, "_interrupt_message", None) != message:
                return
            agent._interrupt_requested = False
            agent._interrupt_message = None
            getattr(agent, "_hard_interrupt_requested", threading.Event()).clear()
            agent._interrupt_thread_signal_pending = False
            if agent._execution_thread_id is not None:
                _set_interrupt(False, agent._execution_thread_id)

    def _stop_on_exhausted_authority(self) -> bool:
        """State.db stayed locked until no renewal can land before the row expires: stop now."""
        logger.warning("Session turn lease refresh stayed locked through its lifetime: %s", self._current_session_id())
        self._interrupt_turn("Session turn lease could not be refreshed; stopping to protect the transcript.")
        return False

    def _renew_after_lapse(self, started: float):
        """[daveey] The tick ran only after the row's expiry had passed, so this process was not
        running for longer than the lease TTL. In practice that is macOS sleep: the scheduler waits
        on the monotonic clock, which stops during sleep, while the row's wall-clock expiry keeps
        running. Every logged "stayed locked through its lifetime" lined up with a wake from
        clamshell or maintenance sleep. Killing the turn on wake throws away work that nobody else
        claimed.

        Expiry only makes the row reclaimable. The holder-qualified UPDATE is a compare-and-swap,
        serialised with acquisition by BEGIN IMMEDIATE. If a successor reclaimed the row (or claimed
        and released it), the holder no longer matches and the renewal misses, so the lease is
        really lost and the turn stops. Otherwise this turn still owns the session and keeps
        running. The transcript-write fence (``hermes_state_messages``) already renews a lapsed row
        that still names its holder; this brings the refresher into line with it. A lock error here
        is a missed tick, retried next interval: the row is already reclaimable, so stopping would
        not protect anything, and holder fencing still refuses the transcript writes of a turn
        whose row a successor takes."""
        session_id = self._current_session_id()
        lapsed_s = max(0.0, started - self._authority_deadline)
        try:
            renewed = self.db.refresh_session_turn_lease(
                session_id, self.holder, ttl_seconds=LEASE_TTL_SECONDS, patience_s=_REFRESH_WRITE_PATIENCE_S,
            )
        except Exception as exc:
            if self.stop.is_set():
                return False
            from hermes_state_errors import is_sqlite_lock_error

            if is_sqlite_lock_error(exc):
                if started < self._authority_deadline:
                    # Not lapsed yet (a tick inside the expiry margin): same rule as a locked
                    # renewal near expiry, stop before the row becomes reclaimable.
                    return self._stop_on_exhausted_authority()
                logger.warning(
                    "Session turn lease lapsed %.0fs ago and its renewal hit a SQLite lock; will retry: %s",
                    lapsed_s, session_id,
                )
                return None
            logger.warning("Failed to renew lapsed session turn lease: %s", session_id, exc_info=True)
            self._interrupt_turn("Session turn lease could not be refreshed; stopping to protect the transcript.")
            return False
        if renewed:
            self._authority_deadline = started + LEASE_TTL_SECONDS
            logger.warning(
                "Session turn lease renewed %.0fs after it lapsed (process suspended or starved; "
                "no other owner claimed the session): %s", lapsed_s, session_id,
            )
            return None
        if self.stop.is_set():
            return False
        logger.error(
            "Lost session turn lease while turn is active (another owner claimed it during a %.0fs lapse): %s",
            lapsed_s, session_id,
        )
        self._interrupt_turn("Session turn lease lost; stopping to protect the transcript.")
        return False

    def refresh_tick(self):
        """One periodic renewal (every ``refresh_interval`` via the shared scheduler). A real
        miss (rowcount 0) or a non-lock error interrupts the turn. A SQLite lock is a missed
        tick only while the next attempt still lands before ``_authority_deadline``. Returning
        False stops the timer.

        Acquisition reclaims a row once ``expires_at <= now``, even if the old process is
        alive. Holder-qualified release does not block that path, so a run of lock errors
        must not keep this turn active up to and past the deadline. A successful renewal
        is the only thing that moves the deadline.

        The holder-qualified UPDATE fences a late refresher from a successor lease. The façade's
        finally sets ``stop`` before releasing, so a holder-fenced miss observed after stop is not
        a loss."""
        if self.stop.is_set():
            return False
        started = time.time()
        # The whole renewal must finish before the authority runs out: a renewal still waiting on the
        # write lock past expiry keeps this turn running while a successor reclaims the row. The
        # margin covers one BEGIN IMMEDIATE that blocks in SQLite's busy handler past the patience.
        patience = self._authority_deadline - _REFRESH_EXPIRY_MARGIN_S - started
        if patience <= 0:
            return self._renew_after_lapse(started)
        try:
            if self.db.refresh_session_turn_lease(
                self._current_session_id(), self.holder, ttl_seconds=LEASE_TTL_SECONDS,
                patience_s=min(_REFRESH_WRITE_PATIENCE_S, patience),
            ):
                self._authority_deadline = started + LEASE_TTL_SECONDS
                return None
            if self.stop.is_set():
                return False
            logger.error(
                "Lost session turn lease while turn is active: %s", self._current_session_id()
            )
            self._interrupt_turn("Session turn lease lost; stopping to protect the transcript.")
        except Exception as exc:
            if self.stop.is_set():
                return False
            from hermes_state_errors import is_sqlite_lock_error

            if is_sqlite_lock_error(exc):
                # The scheduler will not try again until one interval from now. If that attempt
                # could not finish before the row expiry, stop before a successor can claim it.
                if time.time() + self.refresh_interval + _REFRESH_EXPIRY_MARGIN_S >= self._authority_deadline:
                    return self._stop_on_exhausted_authority()
                logger.warning(
                    "Session turn lease refresh hit a SQLite lock; will retry: %s",
                    self._current_session_id(),
                )
                return None
            logger.warning(
                "Failed to refresh session turn lease: %s", self._current_session_id(), exc_info=True,
            )
            self._interrupt_turn(
                "Session turn lease could not be refreshed; stopping to protect the transcript."
            )
        return False


@dataclass
class TurnLeaseAdmission:
    """Outcome of ``admit_durable_turn_lease``: exactly one of ``lease`` / ``early_result`` may be set."""

    lease: Optional[DurableTurnLease] = None
    early_result: Optional[dict[str, Any]] = None
    conversation_history: Optional[list[dict[str, Any]]] = None


def _committed_lease_expiry(db, session_id: str, holder: str, floor: float) -> float:
    """The admitted row's committed ``expires_at``; when unreadable, ``floor + TTL`` (taken before
    acquisition, so never later than the real expiry). Overstating it lets a successor reclaim the
    row while this turn still believes it holds the lease."""
    reader = getattr(db, "session_turn_lease_expires_at", None)
    if callable(reader):
        try:
            committed = reader(session_id, holder)
            if isinstance(committed, (int, float)):
                return float(committed)
        except Exception:
            logger.debug("Could not read the admitted turn lease expiry; using the pre-acquire floor", exc_info=True)
    return floor + LEASE_TTL_SECONDS


def _durable_session_exists(db, session_id: str) -> Optional[bool]:
    """True / False when the row read answered; None when it failed and the state is unknown."""
    try:
        return db.get_session(session_id) is not None
    except Exception:
        # A locked / non-WAL read proves neither presence nor absence: get_session returns None —
        # it does not raise — when the row is missing. See #84234.
        logger.warning(
            "Could not check durable session after turn lease admission; row state is unknown",
            exc_info=True,
        )
        return None


def _seed_adopted_row_model_config(agent, db, session_id: str) -> None:
    """Fill the init model_config keys an existing row lacks (never overwrites); best effort."""
    if getattr(agent, "_persist_disabled", False):
        return
    seed = getattr(db, "seed_session_model_config", None)
    row_config = getattr(agent, "_session_row_model_config", None)
    if not callable(seed) or not callable(row_config):
        return
    try:
        seed(session_id, row_config())
    except Exception:
        logger.warning("Could not record the agent's model_config on an adopted session row", exc_info=True)


def session_was_taken_over(agent, session_id: str) -> bool:
    """The stand-down latch follows compression, but never a different profile or fork."""
    taken = getattr(agent, "_taken_over_session_id", None)
    if not taken or not session_id:
        return False
    db = getattr(agent, "_session_db", None)
    bound_db = getattr(agent, "_taken_over_db_path", None)
    if bound_db is not None and bound_db != getattr(db, "db_path", None):
        return False
    if taken == session_id:
        return True
    if not callable(getattr(type(db), "_session_turn_lease_key", None)):
        return False
    # Read errors propagate: a failed lineage lookup must not authorize a writer.
    return db._session_turn_lease_key(taken) == db._session_turn_lease_key(session_id)


def refuses_held_lease_at_once(agent, platform) -> bool:
    """True for a Desktop/TUI turn its own client submitted: a lease another writer holds is refused
    at once, because that client still has the text and offers Take over.

    A gateway-hosted turn (``gateway_session_key`` set by the gateway's agent build) differs even when
    its session's surface is ``desktop``/``tui`` (``session_policy.SURFACES``): the gateway's FIFO
    already accepted the input (a typed send forwarded to the owner, a cross-session relay), so no
    composer holds it any more. It waits for the holder like every other gateway turn, which queues
    the message behind the running turn instead of failing it ``session_busy`` and dropping it."""
    if platform not in {"desktop", "tui"}:
        return False
    return not getattr(agent, "_gateway_session_key", None)


def admit_durable_turn_lease(
    agent, *, session_id: str, relay_turn_id: str, task_context: dict[str, Any],
    conversation_history: Optional[list[dict[str, Any]]],
) -> TurnLeaseAdmission:
    """Acquire the session turn lease (the row need not exist yet); build (not start) its threads.

    Mutates ``task_context["session_id"]`` and ``agent.session_id`` when the wait forced a resume-id
    reload. Returns an ``early_result`` (interrupted / timed out) instead of a lease when admission
    fails; the caller returns it verbatim."""
    db = getattr(agent, "_session_db", None)
    admission = TurnLeaseAdmission(conversation_history=conversation_history)
    if session_was_taken_over(agent, session_id):
        admission.early_result = {
            "messages": list(conversation_history or []), "api_calls": 0,
            "completed": False, "failed": True, "interrupted": True,
            "_hard_interrupted": True,
            "failure_reason": "session_busy", "failure_retryable": False,
            "final_response": (
                "This chat was taken over by another Hermes window. "
                "Resume it explicitly to continue here. Your message was not processed."
            ),
        }
        return admission
    if db is None or not session_id:
        return admission
    # Check the concrete type: MagicMock-style shims accept any attribute without the protocol.
    if (
        getattr(agent, "_persist_disabled", False)
        or not callable(getattr(type(db), "acquire_session_turn_lease", None))
    ):
        return admission
    # A session id without a row still takes the lease: client-addressed ids (API server
    # X-Hermes-Session-Id, /v1/runs session_id, fingerprint-derived chat ids) are not
    # process-unique, and the first turn creates the row mid-turn, so a second writer would
    # otherwise find the row, take an unheld lease and interleave its turn into this one.
    # pidns stamp: see hermes_state_pidns.
    holder = (
        f"pid={os.getpid()}{holder_namespace_token()}:turn={relay_turn_id}"
        f":platform={task_context['platform'] or 'unknown'}"
    )
    reload_needed = announced = False

    def _on_contended() -> None:
        # A busy state.db, not a known holder: say nothing, but still reload after admission,
        # since the busy writer may have been the previous holder's final flush (one that fits
        # inside the write patience never gets here, before or after this signal existed).
        nonlocal reload_needed
        reload_needed = True

    def _on_wait(elapsed: float) -> None:
        nonlocal reload_needed, announced
        reload_needed = announced = True
        agent._emit_status(
            "⏳ Another Hermes process is using this session; "
            "waiting for it to finish before starting your turn..."
            if elapsed < 1.0 else
            f"⏳ Still waiting for the other Hermes process on this session ({int(elapsed)}s)..."
        )

    authority_floor = time.time()  # every acquisition attempt stamps its expiry after this instant
    if not db.acquire_session_turn_lease(
        session_id, holder, ttl_seconds=LEASE_TTL_SECONDS,
        wait_seconds=0.0 if refuses_held_lease_at_once(agent, task_context['platform']) else LEASE_WAIT_SECONDS,
        on_wait=_on_wait, on_contended=_on_contended,
        should_abort=lambda: getattr(agent, "_interrupt_requested", False),
        # A busy state.db is not another writer: Desktop/TUI skip the holder wait but must not
        # turn a write-lock spike into "This chat has another active writer".
        contended_wait_seconds=BUSY_DB_WAIT_SECONDS,
    ):
        admission.early_result = _lease_not_acquired_result(agent, session_id, conversation_history,
                                                            waited=announced)
        return admission

    # Assign only after admission so the finally cannot release a holder that never owned the
    # row; persist paths read the agent attr so a late flush is fenced in the same transaction.
    lease = DurableTurnLease(
        agent, db, session_id, holder, expires_at=_committed_lease_expiry(db, session_id, holder, authority_floor),
    )
    with turn_admission_lock(agent):  # a runtime switch's check-and-publish excludes this (agent_runtime_switch)
        agent._active_session_turn_lease_holder = holder
        # [daveey] Kept after release (the active attr is cleared even when a busy store defers the release): the
        # turn-end outcome refresh discounts only this turn's own lease (hermes_state_outcome.turn_identity).
        agent._last_session_turn_lease_holder = holder
        agent._active_session_turn_lease_ttl_seconds = LEASE_TTL_SECONDS
    try:
        # Read the row only now: the previous holder may have created or deleted it while this
        # turn waited, so an answer from before admission can be stale either way.
        durable = _durable_session_exists(db, session_id)
        if durable and getattr(agent, "_session_db_reserved", False) is not True:
            # Row proven to exist — suppress the redundant create attempt. A missing row leaves
            # the flag alone: the flush heals a row deleted under a live agent (#123583). So does
            # an unknown one: the create is an upsert that never overwrites an existing row.
            # A child reserved only its identity for transcript readers. Its first turn must
            # still persist constructor config (including lineage) and the completed prompt.
            if not getattr(agent, "_session_db_created", False):
                # Another writer made the row (the gateway's bare SessionStore row), so the skipped
                # create was this agent's only chance to record its init model_config: without it a
                # class session's row lost ``model_routing`` and resumed as a concrete pin.
                _seed_adopted_row_model_config(agent, db, agent.session_id)
            agent._session_db_created = True
        # Reload only a transcript that may exist: callers may seed a fresh id in memory before its
        # row is written, and reloading an absent row would erase that seed. An unknown row still
        # reads the transcript after a wait and adopts it only if it returns rows; that read
        # raising ends the turn.
        if reload_needed and durable is not False:
            if announced:
                agent._emit_status("Session is free; loading the latest transcript...")
            # The holder may have compressed/rotated the session while we waited: reload only
            # AFTER admission; an immediate acquisition skips this (needless prompt-cache miss).
            latest_session_id = db.resolve_resume_session_id(session_id)
            if latest_session_id:
                agent.session_id = latest_session_id
                task_context["session_id"] = latest_session_id
            reloaded = db.get_messages_as_conversation(
                agent.session_id, repair_alternation=True, include_row_ids=True
            )
            # Decide on the stored rows alone: an unknown row that reloads nothing keeps the
            # caller's history, which already holds any carried input below.
            if durable or reloaded:
                # A follow-up that aborted an earlier wait carries that turn's never-persisted
                # input only in memory (see carry_unadmitted_user_message); the reload drops it.
                from agent.session_persistence import _PERSIST_AFTER_ADMISSION_INTERRUPT
                reloaded.extend(
                    m for m in (conversation_history or [])
                    if isinstance(m, dict) and m.get(_PERSIST_AFTER_ADMISSION_INTERRUPT)
                    and "_row_id" not in m
                )
                admission.conversation_history = reloaded
        lease.build_threads()
    except BaseException:
        # The façade never saw this lease; release here so an admitted row is not leaked.
        lease.release()
        raise
    admission.lease = lease
    return admission


def carry_unadmitted_user_message(
    early_result: dict[str, Any], user_message: Any, persist_user_message: Any, *,
    timestamp: Optional[float], display_kind: Optional[str], display_metadata: Optional[dict[str, Any]],
    platform_id: Optional[str],
) -> None:
    """A follow-up that interrupted the lease wait must not consume the accepted input: append it to
    the early result's history so the follow-up turn sees it and persists it (the flush honours
    ``_PERSIST_AFTER_ADMISSION_INTERRUPT`` because this turn never owned the lease). A hard stop
    (``/stop``) cancels the input instead."""
    hard_interrupted = early_result.pop("_hard_interrupted", False)
    if hard_interrupted or not early_result.get("interrupted") or user_message in (None, ""):
        return
    from agent.message_metadata import append_message
    from agent.session_persistence import _PERSIST_AFTER_ADMISSION_INTERRUPT

    durable_content = user_message
    if persist_user_message is not None and (
        not isinstance(user_message, list) or isinstance(persist_user_message, list)
    ):
        durable_content = persist_user_message
    deferred_user: dict[str, Any] = {
        "role": "user", "content": durable_content, _PERSIST_AFTER_ADMISSION_INTERRUPT: True,
    }
    if isinstance(user_message, str) and user_message != durable_content:
        deferred_user["api_content"] = user_message
    if display_kind:
        deferred_user["display_kind"] = display_kind
    if display_metadata:
        deferred_user["display_metadata"] = display_metadata
    if platform_id is not None:
        deferred_user["platform_message_id"] = platform_id
    append_message(early_result["messages"], deferred_user, timestamp=timestamp)


def _lease_not_acquired_result(agent, session_id: str, conversation_history, *, waited: bool) -> dict[str, Any]:
    """``waited``: another holder was seen (the user was told so), not merely an interrupt that
    landed before the first acquisition attempt, which no other process was involved in."""
    base = {"messages": list(conversation_history or []), "api_calls": 0, "completed": False}
    if getattr(agent, "_interrupt_requested", False):
        logger.info("session turn lease wait aborted by interrupt: %s", session_id)
        hard_event = getattr(agent, "_hard_interrupt_requested", None)
        hard_interrupted = bool(
            callable(getattr(hard_event, "is_set", None)) and hard_event.is_set()
        )
        result = {
            "final_response": (
                "Stopped waiting for another Hermes process on this session. "
                "Your message was not processed."
                if waited else
                "Stopped before this turn started. Your message was not processed."
            ),
            **base,
            "interrupted": True,
        }
        if hard_interrupted:
            result["_hard_interrupted"] = True
        if getattr(agent, "_interrupt_message", None):
            result["interrupt_message"] = agent._interrupt_message
        # The finalizer never runs on this early return; clear so a cached agent doesn't
        # fail-close the next turn.
        try:
            agent.clear_interrupt()
        except Exception:
            agent._interrupt_requested = False
            agent._interrupt_message = None
        return result
    # Fail closed like gateway TurnLeaseTimeoutError: surface a resend notice, not a bare TimeoutError.
    timeout_msg = (
        "⏳ Another Hermes process kept this session busy too long. Your message was not "
        "processed - wait for the other process to finish, then send it again."
    )
    if refuses_held_lease_at_once(agent, getattr(agent, "platform", None)):
        timeout_msg = (
            "This chat has another active writer. Use session.take_over (Take over here), "
            "then retry your message. Your message was not processed."
        )
    logger.error("session turn lease wait timed out for %s", session_id)
    try:
        agent._emit_warning(timeout_msg)
    except Exception:
        logger.debug("Failed to emit session turn lease timeout warning", exc_info=True)
    # Stamped so Desktop/TUI show "session busy, send again" instead of code="unknown".
    return {
        "final_response": timeout_msg,
        **base,
        "failed": True,
        "error": f"session_turn_lease_timeout:{session_id}",
        "failure_reason": "session_busy",
        "failure_retryable": True,
    }
