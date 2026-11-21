"""Process-owned admission fence for cooperative Desktop backend retirement.

The gate belongs to the process, not a profile or transport. Diagnostic ledgers alone cannot
close the check/use race: every source of new work must reserve admission before dispatch.
"""

import contextlib
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import _thread
import secrets
import threading
import time

# [daveey] A busy admission.lock is not an update hold (agent-6f2f71c662): hermes-sync's state read,
# its hold write and Kanban claims hold it past one 50 ms try on a loaded Mac, and every such refusal
# dropped a typed message, notification or goal turn. Turn sources retry a busy lock (50 ms tries) for up
# to BUSY_WAIT_S; a real hold or a retiring backend still refuses at once. The one caller that reserves
# under the session resume lock (prompt.admit) passes busy_wait_s=0 and keeps a single try.
# A hub Kanban claim holds the lock for its ~1 s CPU sample (worker-first overflow, frozen contract): the budget
# outlasts that longest designed hold, and a waiting turn makes further claims defer (admission_fence.turn_waiting),
# so a chat turn waits at most about one hold and is never refused by one (review F1). prompt.admit waits outside
# the resume lock instead (methods_prompt_admission).
from hermes_cli.admission_fence import CLAIM_MAX_HOLD_S

BUSY_WAIT_S = CLAIM_MAX_HOLD_S + 0.5
_refusal: ContextVar = ContextVar("update_admission_refusal", default=None)
_busy_wait_override: ContextVar = ContextVar("update_admission_busy_wait", default=None)
_log = logging.getLogger(__name__)


@contextmanager
def single_lock_try():
    """Reservations made inside (this context only) take one short lock try, no busy-lock retry.

    For code that reserves while holding the session resume lock without owning the call site's
    signature (warm session.resume scheduling auto-continue)."""
    token = _busy_wait_override.set(0.0)
    try:
        yield
    finally:
        _busy_wait_override.reset(token)


def last_refusal():
    """Why this context's latest ``reservation`` refused: "retiring", "held", "busy", "unavailable", or None."""
    return _refusal.get()


@contextmanager
def admission_waiter(home=None, busy_wait_s=None):
    """Wait, registered as a waiting turn, until admission.lock is free (at most ``busy_wait_s``, default BUSY_WAIT_S).

    For a caller whose reservation must take one short try because it holds the session resume lock (prompt.admit):
    it waits here with that lock RELEASED, then retries its admission inside this block, so claims keep deferring to
    it until it has reserved. Yields True once the lock came free, False when the budget ran out. [daveey]"""
    from hermes_constants import get_default_hermes_root, get_hermes_home
    from hermes_cli import admission_fence

    root = get_default_hermes_root(home=home or get_hermes_home())
    budget = BUSY_WAIT_S if busy_wait_s is None else busy_wait_s
    with contextlib.ExitStack() as waiting:
        try:
            waiting.enter_context(admission_fence.turn_waiting(root))
        except OSError:
            _log.debug("could not register as a waiting turn", exc_info=True)
        deadline = time.monotonic() + budget
        free = False
        while not free:
            try:
                with admission_fence.admission_lock(root, timeout=0.05):
                    free = True
            except admission_fence.AdmissionBusy:
                if time.monotonic() >= deadline:
                    break
            except (OSError, TimeoutError):
                _log.warning("update admission unavailable while waiting for a busy lock", exc_info=True)
                break
        yield free


def update_hold_started_at(home=None):
    """When the update hold that ``reservation`` refuses on was set (Unix seconds), or None when unknown.

    Reads the same installation-root marker ``admission_fence.reserve`` checks (``holds/hermes-sync``), not
    the Kanban ``drain-hold`` record: that is a different hold with its own clock. Never raises."""
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.admission_fence import hold_started_at

        return hold_started_at(get_default_hermes_root(home=home or get_hermes_home()) / "core-update")
    except Exception:  # health: allow BLE001 -- a display timestamp must never fail the submit it labels
        _log.debug("update hold start time unreadable", exc_info=True)
        return None


def refusal_text():
    """The 5035 text for the latest refusal in this context."""
    if _refusal.get() == "busy":
        return "update admission is briefly busy (another Hermes process holds the update lock); retry"
    return "backend is retiring or update admission is paused; retry after the update"


def _reserve(root, busy_wait_s, what):
    """``admission_fence.reserve`` that rides out a briefly busy lock. Returns (reservation | None, reason).

    Each try keeps reserve's own 50 ms lock cap, so a caller never blocks longer than busy_wait_s + 50 ms.
    A hold returns (None, "held") at once; any other fence error fails closed with its traceback."""
    from hermes_cli import admission_fence
    from hermes_cli.worker_drain_fence import DrainRefused

    started = time.monotonic()
    tries = 0
    with contextlib.ExitStack() as waiting:
        while True:
            tries += 1
            try:
                pending = admission_fence.reserve(root)
            except DrainRefused as exc:
                return None, exc.reason
            except admission_fence.AdmissionBusy:
                waited = time.monotonic() - started
                if waited >= busy_wait_s:
                    _log.warning("%s unavailable: update admission lock busy for %.1fs (no update hold)", what, waited)
                    return None, "busy"
                if tries == 1:
                    try:  # claims defer to a waiting turn; failing to register only loses that priority
                        waiting.enter_context(admission_fence.turn_waiting(root))
                    except OSError:
                        _log.debug("could not register as a waiting turn", exc_info=True)
                continue
            except (OSError, TimeoutError):
                _log.warning("%s unavailable", what, exc_info=True)
                return None, "unavailable"
            break
    if tries > 1:
        _log.debug("update admission lock was busy for %.2fs; %s after %d tries",
                   time.monotonic() - started, "admitted" if pending is not None else "held", tries)
    return pending, (None if pending is not None else "held")



def _describe(pending, purpose, session=None, *, work=None, thread=None):
    """Label a reservation for ``admission_fence state-details``; a test double without the method is fine."""
    describe = getattr(pending, "describe", None)
    if describe is not None:
        extra = {k: v for k, v in (("work", work), ("thread", thread)) if v is not None}
        describe(purpose, session, **extra)


# [daveey] Background reviews (the post-turn memory/skill review fork) run on a daemon thread with
# no session turn lease and no admission reservation, so the idle proof read a backend mid-review
# as idle and retirement hard-interrupted the review mid-call (2026-10-28 04:38, two reviews lost).
# A running review now holds the fence. Inside BACKGROUND_REVIEW_WAIT_S it is left to finish; past it
# a pending retirement asks it to stop before its next provider call (never mid-call); past
# BACKGROUND_REVIEW_HARD_CAP_S a wedged review no longer holds retirement, so it cannot starve it.
BACKGROUND_REVIEW_WAIT_S = 300.0
BACKGROUND_REVIEW_HARD_CAP_S = 900.0
# [daveey] Why a review was asked to stop (``BackgroundReviewHold.wind_down_reason``); the fork's turn exit
# reason is ``review_<reason>_wind_down``.
WIND_DOWN_RETIREMENT = "retirement"
WIND_DOWN_UPDATE_PAUSE = "update_pause"
# [daveey] A review mid provider call reads the drain's pause request only when that call returns (minutes on
# a long call) and holds its admission reservation meanwhile, so the update waited on it and gave up
# (David-MBP 2026-11-20 22:54: "admission reservations busy" from one background_review, update deferred
# 60 m). Once a live pause request has stood REVIEW_UPDATE_PAUSE_GRACE_S, the review's watcher interrupts it
# mid-call: the calls that already ran have landed, and the spawner requeues the review for after the update.
REVIEW_UPDATE_PAUSE_GRACE_S = 5.0
_REVIEW_PAUSE_POLL_S = 1.0


class BackgroundReviewHold:
    """One running review's claim on the fence; ``wind_down`` asks the fork to stop between calls.

    ``root`` is the installation root whose update fence the review reserved (None where the fence is
    unsupported): ``review_stop_reason`` reads that root's core-update pause request."""

    def __init__(self, started_at, reservation=None, root=None, session=None):
        self.started_at = started_at
        self.wind_down = threading.Event()
        self.wind_down_reason = None
        self.reservation = reservation
        self.root = root
        self.session = session
        self.interrupted = False
        # The review actually stopped for an update (broke at a step boundary, or its call was interrupted), so it
        # is requeued. A wind-down label alone is not enough: the call in flight may have been its last.
        self.stopped_for_update = False
        self.review_agent = None  # the review fork, once built (agent.background_review); interrupted on update
        self.ended = threading.Event()
        # Serializes an update stop against the review's end: a review that finished is never re-labelled
        # update-paused (its spawner would requeue a completed review).
        self.stop_lock = threading.Lock()


def review_stop_reason(hold):
    """Why the review holding ``hold`` must stop before its next provider call, or None to go on.

    [daveey] t_01a1d521213ff881: called at every review step boundary. A core-update drain window's pause
    request (``admission_fence.pause_requested``: the request AND the hold, before its expiry) sets the
    review's ``wind_down``, as a pending retirement does past BACKGROUND_REVIEW_WAIT_S. The review held its
    admission reservation for its whole run and read only the retirement signal, so hermes-sync's worker
    reclaim, which waits for every reservation, never opened while one long review ran and the update gave
    up at its 3-minute claim hold (David-MBP 2026-11-13 05:25). Here only between calls; a call still in flight
    after REVIEW_UPDATE_PAUSE_GRACE_S is interrupted by ``watch_update_pause``. Writes from the calls that already
    ran have landed, and the spawner requeues the review to run after the update."""
    wind_down = getattr(hold, "wind_down", None)
    if wind_down is None:
        return None
    root = getattr(hold, "root", None)
    if not wind_down.is_set() and root is not None:
        from hermes_cli.admission_fence import pause_requested

        if pause_requested(root):
            hold.wind_down_reason = WIND_DOWN_UPDATE_PAUSE
            wind_down.set()
            _log.info("Background review (session=%s) stopping before its next provider call: a Hermes update "
                      "asked turns to pause", str(getattr(hold, "session", None) or "?")[-12:])
    if wind_down.is_set():
        reason = getattr(hold, "wind_down_reason", None) or WIND_DOWN_RETIREMENT
        if reason == WIND_DOWN_UPDATE_PAUSE:
            hold.stopped_for_update = True  # the fork breaks here, before its next provider call
        return reason
    return None


def stop_review_for_update(hold, *, why="after a pause request stood its grace"):
    """Wind ``hold``'s review down for a core update; interrupt its in-flight provider call unless a tool is running
    (a memory/skill write is short and must not be cut: the review then stops at the boundary after it). Returns
    whether the review is (now) stopping for the update: False once it ended, or when a retirement already wound it
    down (that path keeps its own reason and requeue rules). Never raises."""
    with hold.stop_lock:
        if hold.ended.is_set():
            return False
        if hold.wind_down.is_set() and hold.wind_down_reason != WIND_DOWN_UPDATE_PAUSE:
            return False
        hold.wind_down_reason = WIND_DOWN_UPDATE_PAUSE
        hold.wind_down.set()
    agent = hold.review_agent
    if agent is None:  # not built yet: it stops at its first step boundary (review_stop_reason)
        return True
    try:
        from agent.update_pause_cancel import COMPRESSION_CALL_ID, MODEL_CALL_ID, in_flight

        # Only real tool calls wait: the model-request and compression markers are the provider calls this cuts.
        if any(call not in (MODEL_CALL_ID, COMPRESSION_CALL_ID) for call in in_flight(agent)):
            return True  # the watcher retries the interrupt once the tool returns
        if getattr(hold, "interrupted", False):
            return True
        from agent.interrupt_compat import request_hard_interrupt
        from agent.interrupt_control import _REASON_UPDATE_PAUSE

        with hold.stop_lock:  # a review that ended meanwhile finished on its own: it is not requeued
            if hold.ended.is_set():
                return False
            hold.interrupted = hold.stopped_for_update = True
        request_hard_interrupt(agent, "stopped for a Hermes update", tool_reason=_REASON_UPDATE_PAUSE)
    except Exception:  # health: allow BLE001 -- the wind-down above still stops it at the next boundary
        _log.warning("Background review interrupt for a Hermes update failed", exc_info=True)
        return True
    _log.info("Background review (session=%s) interrupted for a Hermes update (%s); it is requeued to run after "
              "the update", str(hold.session or "?")[-12:], why)
    return True


def start_update_pause_watcher(hold):
    """Start ``hold``'s update-pause watcher; it exits when the review ends (``end_background_review``). Called
    from the review thread itself, so a review whose thread never runs never leaves a watcher behind. Never raises:
    without a watcher the review still stops at its next step boundary (``review_stop_reason``)."""
    if hold is None or getattr(hold, "root", None) is None or hold.ended.is_set():
        return
    try:
        # A low-level thread: never a threading.Thread a test swapped for a synchronous fake (that would run this
        # polling loop inline and hang the review), never local-work custody (a drain would wait on its own
        # watcher), and it never blocks interpreter exit. Infrastructure: no profile-scoped work.
        _thread.start_new_thread(watch_update_pause, (hold,))
    except RuntimeError:
        _log.warning("Background review update-pause watcher did not start", exc_info=True)


def watch_update_pause(hold, *, poll_s=None, grace_s=None):
    """Poll ``hold.root``'s pause request until the review ends; stop the review once the request has stood
    ``grace_s``. A request that lapses resets the grace. Runs on its own low-level thread (``start_update_pause_watcher``, from the review thread)."""
    from hermes_cli.admission_fence import pause_requested

    poll_s = _REVIEW_PAUSE_POLL_S if poll_s is None else poll_s
    grace_s = REVIEW_UPDATE_PAUSE_GRACE_S if grace_s is None else grace_s
    seen = None
    while not hold.ended.wait(poll_s):
        if not pause_requested(hold.root):
            seen = None
            continue
        now = time.monotonic()
        seen = now if seen is None else seen
        if now - seen >= grace_s:
            if not stop_review_for_update(hold) or getattr(hold, "interrupted", False):
                return
            # A tool was running (or the fork is not built yet): retry the interrupt on the next poll.


class RetirementFence:
    """Exclusive 30-second prepare permit; commit closes admission permanently.

    Tokens are random and recognized only by this process instance. A committed prepare
    returns the same token so a client can recover a lost commit response without reopening.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._preparing = False
        self._token = None
        self._committed = False
        self._expires_at = 0.0
        self._now = time.monotonic
        self._reviews = set()

    def _expire(self):
        if self._token is not None and not self._committed and self._now() >= self._expires_at:
            self._token = None

    def acquire(self):
        with self._lock:
            self._expire()
            if self._preparing or self._token is not None:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            self._active -= 1

    def active_count(self):
        with self._lock:
            return self._active

    def retired(self):
        """Committed retirement is permanent; prepare alone is reversible."""
        with self._lock:
            return self._committed

    def retiring(self):
        """A retirement is preparing or holds a live/committed permit: admission is closed or closing."""
        with self._lock:
            self._expire()
            return self._preparing or self._token is not None

    def begin_background_review(self, session_id=None):
        """Admit a review under BOTH the process and cross-process update fences.

        The sync drainer cannot see this process's review set. Keep a locked
        admission reservation until the review thread exits, including mid-call
        waits. A drain window closes new admissions and waits for this claim;
        its pause request stops the review before its next provider call
        (``review_stop_reason``). Only a single call that outlasts the window
        makes the window expire rather than killing it. The reservation names
        the review and its session for the drainer's ``state-details`` read.
        """
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.admission_fence import SUPPORTED

        pending = root = None
        if SUPPORTED:
            root = get_default_hermes_root(home=get_hermes_home())
            pending, _reason = _reserve(root, BUSY_WAIT_S, "Review update admission")
            if pending is None:
                return None
            _describe(pending, "background_review", session_id)
        with self._lock:
            self._expire()
            if self._preparing or self._token is not None:
                if pending is not None:
                    pending.close()
                return None
            hold = BackgroundReviewHold(self._now(), pending, root, session_id)
            self._reviews.add(hold)
        return hold

    def end_background_review(self, hold):
        if hold is None:
            return
        # Close outside the process lock. Reservation.close is idempotent and
        # keeps the file locked until unlink, so there is no false-idle gap.
        with self._lock:
            self._reviews.discard(hold)
        ended = getattr(hold, "ended", None)
        if ended is not None:
            with hold.stop_lock:  # after this no update stop can relabel the finished review
                ended.set()  # also stops the review's update-pause watcher
        if hold.reservation is not None:
            hold.reservation.close()

    def _blocking_reviews(self):
        now = self._now()
        return [h for h in self._reviews if now - h.started_at < BACKGROUND_REVIEW_HARD_CAP_S]

    def background_review_count(self):
        """Running reviews that still hold retirement (younger than the hard cap)."""
        with self._lock:
            return len(self._blocking_reviews())

    @contextmanager
    def work(self):
        admitted = self.acquire()
        try:
            yield admitted
        finally:
            if admitted:
                self.release()

    @contextmanager
    def reservation(self, home=None, *, busy_wait_s=None, session=None, work=None, thread=None):
        """Compose process retirement with the cross-process update reservation.

        Neither lock is held while acquiring the other, or while callers hold
        session locks. The pending reservation remains visible until publication.
        A briefly busy admission.lock is retried for ``busy_wait_s`` (default
        BUSY_WAIT_S; 0 = one 50 ms try, for callers holding the session resume
        lock, also forced by ``single_lock_try()``); the refusal reason is
        readable afterwards through ``last_refusal()``. ``session``/``work``/``thread`` [daveey] name the
        holder in the reservation file (``admission_fence state-details``).
        """
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.admission_fence import HOLD_MESSAGE, SUPPORTED

        if busy_wait_s is None:
            override = _busy_wait_override.get()
            busy_wait_s = BUSY_WAIT_S if override is None else override
        if not SUPPORTED:
            with self.work() as admitted:
                _refusal.set(None if admitted else "retiring")
                yield admitted
            return

        admitted = self.acquire()
        pending = None
        try:
            reason = "retiring"
            if admitted:
                pending, reason = _reserve(get_default_hermes_root(home=home or get_hermes_home()),
                                           busy_wait_s, "Update admission")
                if reason == "held":
                    _log.info(HOLD_MESSAGE)
                elif pending is not None:
                    _describe(pending, "turn_admission", session, work=work, thread=thread)
            _refusal.set(reason)
            yield admitted and pending is not None
        finally:
            try:
                if pending is not None:
                    pending.close()
            finally:
                if admitted:
                    self.release()

    def prepare(self):
        from hermes_cli.web_server_idle_proof import idle_proof

        with self._lock:
            self._expire()
            if self._committed:
                return {"ok": True, "idle": True, "token": self._token}
            if self._preparing or self._token is not None or self._active:
                return {"ok": False, "idle": False}
            reviews = self._blocking_reviews()
            if reviews:
                now = self._now()
                if any(now - h.started_at >= BACKGROUND_REVIEW_WAIT_S for h in reviews):
                    for hold in reviews:
                        if not hold.wind_down.is_set():
                            hold.wind_down_reason = WIND_DOWN_RETIREMENT
                        hold.wind_down.set()
                return {"ok": False, "idle": False}
            self._preparing = True
        # No admissions can cross the freeze. Do not hold our lock while reading ledgers:
        # some callers already hold a session/cron lock when reserving their work.
        verdict = None
        try:
            verdict = idle_proof()["idle"]
        except Exception:
            logging.getLogger(__name__).warning("Retirement idle probe unavailable", exc_info=True)
        finally:
            with self._lock:
                self._preparing = False
                if verdict is True:
                    self._token = token = secrets.token_urlsafe(32)
                    self._expires_at = self._now() + 30.0
        if verdict is not True:
            return {"ok": False, "idle": verdict}
        return {"ok": True, "idle": True, "token": token}

    def commit(self, token):
        with self._lock:
            self._expire()
            if not token or token != self._token:
                return {"ok": False}
            self._committed = True
            return {"ok": True}

    def cancel(self, token):
        with self._lock:
            self._expire()
            if not token or token != self._token or self._committed:
                return {"ok": False}
            self._token = None
            return {"ok": True}


retirement = RetirementFence()
