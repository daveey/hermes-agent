"""Process-owned admission fence for cooperative Desktop backend retirement.

The gate belongs to the process, not a profile or transport. Diagnostic ledgers alone cannot
close the check/use race: every source of new work must reserve admission before dispatch.
"""

from contextlib import contextmanager
import logging
import secrets
import threading
import time

# [daveey] Background reviews (the post-turn memory/skill review fork) run on a daemon thread with
# no session turn lease and no admission reservation, so the idle proof read a backend mid-review
# as idle and retirement hard-interrupted the review mid-call (2026-10-28 04:38, two reviews lost).
# A running review now holds the fence. Inside BACKGROUND_REVIEW_WAIT_S it is left to finish; past it
# a pending retirement asks it to stop before its next provider call (never mid-call); past
# BACKGROUND_REVIEW_HARD_CAP_S a wedged review no longer holds retirement, so it cannot starve it.
BACKGROUND_REVIEW_WAIT_S = 300.0
BACKGROUND_REVIEW_HARD_CAP_S = 900.0


class BackgroundReviewHold:
    """One running review's claim on the fence; ``wind_down`` asks the fork to stop between calls."""

    def __init__(self, started_at, reservation=None):
        self.started_at = started_at
        self.wind_down = threading.Event()
        self.reservation = reservation


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

    def retiring(self):
        """A retirement is preparing or holds a live/committed permit: admission is closed or closing."""
        with self._lock:
            self._expire()
            return self._preparing or self._token is not None

    def begin_background_review(self):
        """Admit a review under BOTH the process and cross-process update fences.

        The sync drainer cannot see this process's review set. Keep a locked
        admission reservation until the review thread exits, including mid-call
        waits. A drain window closes new admissions and waits for this claim;
        if the call outlasts the window, the window expires rather than killing it.
        """
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.admission_fence import reserve, SUPPORTED

        pending = None
        if SUPPORTED:
            try:
                pending = reserve(get_default_hermes_root(home=get_hermes_home()))
            except (OSError, TimeoutError):
                logging.getLogger(__name__).warning("Review update admission unavailable", exc_info=True)
                return None
            if pending is None:
                return None
        with self._lock:
            self._expire()
            if self._preparing or self._token is not None:
                if pending is not None:
                    pending.close()
                return None
            hold = BackgroundReviewHold(self._now(), pending)
            self._reviews.add(hold)
            return hold

    def end_background_review(self, hold):
        if hold is None:
            return
        # Close outside the process lock. Reservation.close is idempotent and
        # keeps the file locked until unlink, so there is no false-idle gap.
        with self._lock:
            self._reviews.discard(hold)
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
    def reservation(self, home=None):
        """Compose process retirement with the cross-process update reservation.

        Neither lock is held while acquiring the other, or while callers hold
        session locks. The pending reservation remains visible until publication.
        """
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.admission_fence import reserve, HOLD_MESSAGE, SUPPORTED

        if not SUPPORTED:
            with self.work() as admitted:
                yield admitted
            return

        admitted = self.acquire()
        pending = None
        try:
            if admitted:
                try:
                    pending = reserve(get_default_hermes_root(home=home or get_hermes_home()))
                except (OSError, TimeoutError):
                    logging.getLogger(__name__).warning("Update admission unavailable", exc_info=True)
                if pending is None:
                    logging.getLogger(__name__).info(HOLD_MESSAGE)
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
