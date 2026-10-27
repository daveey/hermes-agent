"""Retained admission ownership across asynchronous turn handoffs.

A session owns one reference until terminal settlement; each executing stage owns
another. Clearing the UI's in-flight state must not make unwinding work idle.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import threading

_current: ContextVar[tuple | None] = ContextVar("turn_admission_owner", default=None)
_KEY = "_turn_admission"


class TurnAdmission:
    def __init__(self, reservation):
        self.reservation = reservation
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
                self.reservation.__exit__(None, None, None)


def acquire(home=None):
    from hermes_cli.backend_retirement import retirement
    reservation = retirement.reservation(home)
    if not reservation.__enter__():
        reservation.__exit__(None, None, None)
        return None
    return TurnAdmission(reservation)


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
    token = session.pop(_KEY, None)
    if token is not None:
        if session.get("_turn_admission_generation") is token:
            session.pop("_turn_admission_generation", None)
        token.release()
    return True


@contextmanager
def admission(session):
    token = acquire(session.get("profile_home"))
    with session["history_lock"]:
        claimed = token is not None and not session.get("running")
        if claimed:
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
        session["last_active"] = time.time()
        session.pop("_turn_user_input", None)
        session.pop("_stop_issuer", None)
        if not st.error_retained:
            server._clear_inflight_turn(session)
        server._release_hosted_room_turn_slot(session)


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


def start_work(target, *, name, session=None, home=None):
    from agent.memory_provider import spawn_context_thread
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
    if token is None:
        token = acquire(home or (session or {}).get("profile_home"))
        if token is not None and session is not None:
            with session["history_lock"]:
                existing = session.get(_KEY)
                if existing is not None:
                    token.release()
                    token = existing.retain()
                else:
                    session[_KEY] = token.retain()
                    session["_turn_admission_generation"] = token
    if token is None:
        return None
    with token.lock:
        token.workers += 1
    finished = False
    finish_lock = threading.Lock()

    def finish():
        nonlocal finished
        with finish_lock:
            if finished:
                return
            finished = True
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

    def run():
        context = _current.set((session, token))
        try:
            target()
        finally:
            try:
                finish()
            finally:
                _current.reset(context)

    try:
        thread = spawn_context_thread(run, name=name)
        if session is not None:
            session["_run_thread"] = thread
        thread.start()
        return thread
    except BaseException:
        context = _current.set((session, token))
        try:
            finish()
        finally:
            _current.reset(context)
        raise
