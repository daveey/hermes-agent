"""Retained admission ownership across asynchronous turn handoffs.

A session owns one reference until terminal settlement; each executing stage owns
another. Clearing the UI's in-flight state must not make unwinding work idle.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import threading
from hermes_cli.worker_drain_fence import admit, DrainRefused, lease_scope

_current: ContextVar[tuple | None] = ContextVar("turn_admission_owner", default=None)
_KEY = "_turn_admission"
# [daveey] Set when ``_admit_prompt_turn`` admits a turn, dropped when that turn settles or clears ``running``:
# while it is set and ``running`` holds, a refused submit on the record is a sibling of a live turn and owns none
# of its state. DEFENSIVE: every current caller claims ``running`` before it submits, so no caller reaches a
# refusal over a live turn on its own record today; this guards a future caller that skips the claim.
LIVE_TURN = "_admitted_live_turn"
_spared: ContextVar[bool] = ContextVar("turn_admission_refusal_spared_live_turn", default=False)


def mark_live_turn(session):
    """Caller holds history_lock: an admitted turn now owns ``running``, the admission ref and ``inflight_turn``."""
    session[LIVE_TURN] = object()


def live_turn_holds(session):
    """Caller holds history_lock. True while an admitted turn owns this record's running state."""
    return bool(session.get("running")) and session.get(LIVE_TURN) is not None


def note_refusal(*, spared_live_turn):
    """Record (for this thread's caller of ``_run_prompt_submit``) whether its refusal left a live turn alone."""
    _spared.set(bool(spared_live_turn))


def refusal_spared_live_turn():
    """True when this thread's last admission refusal found a live turn: the caller releases nothing it shares."""
    return _spared.get()


class TurnAdmission:
    def __init__(self, reservation, drain_lease=None):
        self.reservation = reservation
        self.drain_lease = drain_lease
        self.lock = threading.RLock()
        self.refs = 1
        self.workers = 0

    def retain(self):
        with self.lock:
            if not self.refs:
                raise RuntimeError("turn admission already released")
            self.refs += 1
        return self

    def release(self):
        with self.lock:
            self.refs -= 1
            if self.refs == 0:
                try:
                    self.reservation.__exit__(None, None, None)
                finally:
                    if self.drain_lease is not None:
                        self.drain_lease.close()


def acquire(home=None, *, busy_wait_s=None, session=None, work=None):
    from hermes_cli.backend_retirement import retirement
    # [daveey] The reservation file names its holder (hermes-sync's refusal line reads it back).
    label = {"session": str(session.get("session_key") or "") or None if isinstance(session, dict) else None,
             "work": work, "thread": threading.current_thread().name}
    # busy_wait_s=None keeps the fence's default busy-lock retry; 0 is one short try (resume-lock callers).
    reservation = (retirement.reservation(home, **label) if busy_wait_s is None
                   else retirement.reservation(home, busy_wait_s=busy_wait_s, **label))
    try:
        drain_lease = admit('turn.admission')
    except DrainRefused:
        return None
    try:
        with lease_scope(drain_lease):
            if not reservation.__enter__():
                reservation.__exit__(None, None, None)
                if drain_lease is not None:
                    drain_lease.close()
                return None
        return TurnAdmission(reservation, drain_lease)
    except BaseException:
        if drain_lease is not None:
            drain_lease.close()
        raise


def owns(session):
    current = _current.get()
    if current is None or current[0] is not session:
        return True
    # A turn that already settled (generation cleared mid-turn) still owns its own
    # unwinding; only a newer admitted turn's generation makes this worker stale.
    generation = session.get("_turn_admission_generation")
    return generation is None or generation is current[1]


def settle(session):
    """Caller holds history_lock. A stale worker cannot settle a newer turn."""
    if not owns(session):
        return False
    session.pop(LIVE_TURN, None)
    token = session.pop(_KEY, None)
    if token is not None:
        if session.get("_turn_admission_generation") is token:
            session.pop("_turn_admission_generation", None)
        token.release()
    return True


@contextmanager
def admission(session, *, busy_wait_s=None, work="turn-claim"):
    token = acquire(session.get("profile_home"), busy_wait_s=busy_wait_s, session=session, work=work)
    with session["history_lock"]:
        claimed = token is not None and not session.get("running")
        if claimed:
            session.pop(LIVE_TURN, None)  # nothing runs here: a leftover record must not spare this claim's refusal
            old = session.pop(_KEY, None)
            if old is not None:
                old.release()
            session[_KEY] = token
            session["_turn_admission_generation"] = token
        try:
            yield token is not None
        except BaseException:
            if claimed:
                session["running"] = False
                if session.get(_KEY) is token:
                    session.pop(_KEY)
                    if session.get("_turn_admission_generation") is token:
                        session.pop("_turn_admission_generation", None)
                    token.release()
            raise
        else:
            if claimed and not session.get("running") and session.get(_KEY) is token:
                session.pop(_KEY)
                if session.get("_turn_admission_generation") is token:
                    session.pop("_turn_admission_generation", None)
                token.release()
        finally:
            if token is not None and not claimed:
                token.release()


@contextmanager
def stage(session, *, inherit=False):
    """Protect handoff preparation; nested dispatch inherits its creating turn."""
    with session["history_lock"]:
        current = _current.get()
        token = (current[1] if inherit and current is not None and current[0] is session
                 else session.get(_KEY))
        if token is not None:
            token.retain()
    context = _current.set((session, token))
    try:
        with lease_scope(token.drain_lease if token is not None else None):
            yield
    except BaseException:
        with session["history_lock"]:
            if owns(session):
                session["running"] = False
                settle(session)
        raise
    finally:
        _current.reset(context)
        if token is not None:
            token.release()


def retained_callback(session, target, *, settle_on_error=False):
    """Return a one-shot callback and cancellation function sharing one reference."""
    with session.get("history_lock", nullcontext()):
        current = _current.get()
        # A callback belongs to its creating stage, not whichever turn happens
        # to occupy the session after an interrupt and resubmit.
        token = current[1] if current is not None and current[0] is session else session.get(_KEY)
        if token is not None:
            token.retain()
    lock = threading.Lock()
    pending = True

    def cancel():
        nonlocal pending
        with lock:
            if pending:
                pending = False
                if token is not None:
                    token.release()

    def run(*args, **kwargs):
        context = _current.set((session, token))
        try:
            with lease_scope(token.drain_lease if token is not None else None):
                return target(*args, **kwargs)
        except BaseException:
            if settle_on_error:
                with session["history_lock"]:
                    if owns(session):
                        session["running"] = False
                        settle(session)
            raise
        finally:
            _current.reset(context)
            cancel()
    return run, cancel


def settle_prompt_state(session, st):
    import time
    from tui_gateway import server
    with session["history_lock"]:
        if not owns(session):
            return
        st.agent.interim_assistant_callback = None
        session["running"] = False
        session.pop(LIVE_TURN, None)  # the record means "this turn holds running": both end together
        session["last_active"] = time.time()
        session.pop("_turn_user_input", None)
        session.pop("_stop_issuer", None)
        if not st.error_retained:
            server._clear_inflight_turn(session)
        server._release_hosted_room_turn_slot(session)
    from tui_gateway.detached_turn_cleanup import settled
    settled(session)


def start_build_work(session, target, *, delay=None):
    run, cancel = retained_callback(session, target)
    try:
        thread = (threading.Thread(target=run, daemon=True) if delay is None
                  else threading.Timer(delay, run))
        thread.daemon = True
        if delay is None:
            session["_agent_build_thread"] = thread
        thread.start()
        return thread
    except BaseException:
        cancel()
        raise


def start_work(target, *, name, session=None, home=None, busy_wait_s=None):
    prepared = prepare_work(target, name=name, session=session, home=home, busy_wait_s=busy_wait_s)
    return None if prepared is None else prepared.start()


def prepare_work(target, *, name, session=None, home=None, busy_wait_s=None):
    """``start_work`` split in two: retain the admission now (takes ``history_lock``), spawn the worker with
    ``start()`` later without touching ``history_lock``, or give the admission back with ``cancel()``.

    Lock order is ``history_lock`` -> ``_sessions_lock`` (session.resume/activate rebind a transport under
    ``history_lock``, then cancel the orphan reap under ``_sessions_lock``). A caller that must start the
    worker atomically with a ``_sessions_lock`` check prepares BEFORE taking that lock; taking
    ``history_lock`` inside it deadlocked a resume racing its session's own prompt.submit."""
    if session is not None:
        with session["history_lock"]:
            if not owns(session):
                return None
            current = _current.get()
            token = current[1] if current is not None and current[0] is session else session.get(_KEY)
            if token is not None:
                token.retain()
    else:
        token = None
    installed = False
    if token is None:
        token = acquire(home or (session or {}).get("profile_home"), busy_wait_s=busy_wait_s, session=session,
                        work=name)
        if token is not None and session is not None:
            with session["history_lock"]:
                existing = session.get(_KEY)
                if existing is not None:
                    token.release()
                    token = existing.retain()
                else:
                    session[_KEY] = token.retain()
                    session["_turn_admission_generation"] = token
                    installed = True
    if token is None:
        return None
    with token.lock:
        shared = token.workers > 0  # another worker of this turn is running and would have settled it
        token.workers += 1
    return PreparedWork(target, name, session, token, installed, shared)


class PreparedWork:
    """A worker whose admission is retained but whose thread has not started (see ``prepare_work``)."""

    def __init__(self, target, name, session, token, installed, shared):
        self._target, self._name, self._session, self._token = target, name, session, token
        self._installed = installed  # this prepare put ``token`` on the session (no turn owned it yet)
        self._shared = shared  # another worker was running when this one joined the turn
        self._finished = False
        self._finish_lock = threading.Lock()

    def _finish(self):
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
        self._leave()

    def _leave(self):
        session, token = self._session, self._token
        # The final worker also covers failures before normal terminal cleanup.
        if session is not None:
            with session["history_lock"], token.lock:
                token.workers -= 1
                if token.workers == 0 and session.get("_turn_admission_generation") in (None, token) and not session.get("_compute_host_turn_id"):
                    session["running"] = False
                    settle(session)
        else:
            with token.lock:
                token.workers -= 1
        token.release()

    def cancel(self):
        """Never started: leave the turn as if ``start_work`` had not been called (a sibling worker that
        finished meanwhile deferred settlement to this one, so it settles then; otherwise only undo)."""
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
        if self._shared:
            # A sibling that finished meanwhile skipped settlement because this worker was counted; leaving
            # as the last worker settles the turn exactly as that sibling would have.
            self._leave()
            return
        session, token = self._session, self._token
        if session is not None and self._installed:
            with session["history_lock"]:
                if session.get(_KEY) is token:
                    session.pop(_KEY)
                    if session.get("_turn_admission_generation") is token:
                        session.pop("_turn_admission_generation", None)
                    token.release()
        with token.lock:
            token.workers -= 1
        token.release()

    def _run(self):
        context = _current.set((self._session, self._token))
        try:
            with lease_scope(self._token.drain_lease):
                self._target()
        finally:
            try:
                self._finish()
            finally:
                _current.reset(context)

    def abort(self):
        """A ``start`` that raised: the worker's own cleanup (it may settle the turn), from this thread."""
        context = _current.set((self._session, self._token))
        try:
            self._finish()
        finally:
            _current.reset(context)

    def start(self, *, abort_on_error=True):
        """Spawn the worker; takes no session lock. Under ``_sessions_lock`` pass ``abort_on_error=False`` and
        call ``abort()`` after releasing it, since the cleanup takes ``history_lock``."""
        from agent.memory_provider import spawn_context_thread
        try:
            thread = spawn_context_thread(self._run, name=self._name)
            if self._session is not None:
                self._session["_run_thread"] = thread
            thread.start()
            return thread
        except BaseException:
            if abort_on_error:
                self.abort()
            raise
