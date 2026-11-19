"""Explicit physical thread/executor reservations; never monkeypatch stdlib.

Scope propagation and reservation lifetime are distinct: a canceled waiter does
not finish a running target. No helper here claims complete runtime coverage.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import os
import threading
from typing import TYPE_CHECKING
import weakref

from hermes_cli.local_work import ACTIVE_SCOPE, WorkScope, get_registry


class _Reservation:
    """One custodian shared by target-finally and verified pre-start completion."""

    def __init__(self, owner, kind, context):
        self.owner = owner
        self.context = context
        self.token = owner.registry.reserve(kind, home=owner.home, source=owner.source)
        self.lock = threading.Lock()
        self.state = "pending"

    def _finish(self):
        # Caller holds lock; never finish the same registry token twice.
        self.state = "done"
        self.owner.registry.finish(self.token)

    def never_started(self, _future=None):
        with self.lock:
            if self.state == "pending":
                self._finish()

    def run(self, function, args, kwargs):
        with self.lock:
            if self.state != "pending":
                return None
            self.state = "running"
        try:
            return self.context.run(self._invoke, function, args, kwargs)
        finally:
            with self.lock:
                self._finish()

    def _invoke(self, function, args, kwargs):
        token = ACTIVE_SCOPE.set(self.owner)
        try:
            return function(*args, **kwargs)
        finally:
            ACTIVE_SCOPE.reset(token)


def _owner(registry, home, source, kind):
    captured = ACTIVE_SCOPE.get()
    if registry is None and home is None and source is None and captured is not None:
        return captured
    registry = registry if registry is not None else get_registry()
    return WorkScope(registry, kind, home, source or "explicit-physical-work")


def _real_thread_class():
    # Resolve the stdlib class through the main thread's MRO, not the ``threading.Thread``
    # attribute: a first import while a test double replaces that attribute must not make the
    # double this class's base for the rest of the process.
    for cls in type(threading.main_thread()).__mro__:
        if cls.__module__ == "threading" and cls.__name__ == "Thread":
            return cls
    return threading.Thread


if TYPE_CHECKING:
    _RealThread = threading.Thread
else:
    _RealThread = _real_thread_class()


def thread_class_replaced(registry):
    """True when ``threading.Thread`` is not the stdlib class (a test double, a green-thread
    monkeypatch). Physical custody cannot attach to a replaced class, so the caller honours the
    replacement and this latches unknown coverage instead of claiming the work tracked."""
    if threading.Thread is _RealThread:
        return False
    registry.mark_unknown("threading-thread-replaced")
    return True


class TrackedThread(_RealThread):
    """Capture ownership/context at construction; reserve only on first start.

    Use a target, not a run() override: this class owns physical finalization.
    Explicit ownership permits use outside a scope. An unbound home is unknown,
    never silently resolved from process environment.
    """

    def __init__(self, *args, registry=None, home=None, source=None,
                 kind="thread", **kwargs):
        super().__init__(*args, **kwargs)
        self._work_owner = _owner(registry, home, source, kind)
        self._work_kind = kind
        self._work_context = copy_context()
        self._work_start_lock = threading.Lock()
        self._work_reservation = None
        self._work_start_attempted = False

    def start(self):
        with self._work_start_lock:
            if self._work_start_attempted:
                # Do not replace/rollback the first start's physical token.
                raise RuntimeError("threads can only be started once")
            reservation = _Reservation(self._work_owner, self._work_kind,
                                       self._work_context)
            self._work_start_attempted = True
            self._work_reservation = reservation
            try:
                super().start()
            except BaseException:
                # CPython places the object in _limbo before native creation and
                # removes it on native failure; _started alone misses an
                # interrupted wait while the new thread is still bootstrapping.
                limbo_lock = getattr(threading, "_active_limbo_lock", None)
                limbo = getattr(threading, "_limbo", None)
                never_started = False
                if limbo_lock is not None and limbo is not None:
                    with limbo_lock:
                        never_started = not self._started.is_set() and self not in limbo
                if never_started:
                    reservation.never_started()
                    self._work_start_attempted = False
                    self._work_reservation = None
                else:
                    self._work_owner.registry.mark_unknown("thread-start-outcome-uncertain")
                raise

    def run(self):
        reservation = self._work_reservation
        if reservation is None:
            raise RuntimeError("TrackedThread.run requires start")
        return reservation.run(super().run, (), {})


def submit_tracked(executor, function, /, *args, registry=None, home=None,
                   source=None, kind="executor", **kwargs):
    """Reserve before submit and hand custody to physical execution.

    A submit exception may occur AFTER enqueue (stdlib starts new workers after
    queuing). Without a Future there is no cancellation receipt: latch unknown
    and leave custody with the wrapper. If it later executes it releases; if
    execution cannot be reconciled the reservation stays active conservatively.
    """
    owner = _owner(registry, home, source, kind)
    return _submit_owned(executor, function, args, kwargs, owner, kind)


def _refused_before_enqueue(executor):
    """True when a stdlib ThreadPoolExecutor is shut down or broken (its submit refuses pre-enqueue)."""
    if not isinstance(executor, ThreadPoolExecutor):
        return False
    from concurrent.futures import thread as _thread
    return bool(getattr(executor, "_shutdown", False) or getattr(executor, "_broken", False)
                or getattr(_thread, "_shutdown", False))


def _submit_owned(executor, function, args, kwargs, owner, kind):
    reservation = _Reservation(owner, kind, copy_context())
    call = partial(reservation.run, function, args, kwargs)
    # Attribution may inspect the logical target, but must execute custody intact.
    call._la_target = (function, args)  # type: ignore[attr-defined]
    try:
        # Bypass ScopeAwareExecutor.submit to avoid wrapping the same boundary
        # twice. Nested physical work inside the target is still scope-aware.
        if isinstance(executor, ScopeAwareExecutor):
            future = ThreadPoolExecutor.submit(executor, call)
        else:
            future = executor.submit(call)
    except BaseException:
        if _refused_before_enqueue(executor):
            # A stdlib pool refuses a shut-down/broken submit under its lock BEFORE enqueuing.
            # Even if it had raced an enqueue, never_started makes the wrapper a no-op, so
            # releasing here can never undercount physical work.
            reservation.never_started()
        else:
            owner.registry.mark_unknown("submit-outcome-uncertain")
        raise
    # done + pending proves this wrapper never began (cancelled or broken pool).
    # A running wrapper owns its release even if an async waiter was canceled.
    future.add_done_callback(reservation.never_started)
    return future


class ScopeAwareExecutor(ThreadPoolExecutor):
    """Passive by default; scoped submissions transfer captured physical work.

    Install explicitly on an owning loop, not globally. Pass constructor options
    through unchanged, including initializer and thread-name semantics.
    """

    def submit(self, function, /, *args, **kwargs):
        owner = ACTIVE_SCOPE.get()
        if owner is None:
            return super().submit(function, *args, **kwargs)
        return _submit_owned(self, function, args, kwargs, owner, "executor")


# ── Call-site helpers for ingress seams (no-ops while no registry is installed;
# installation/attestation is the bootstrap owner's job, never lazily done here) ──

def installed_registry():
    from hermes_cli import local_work
    return local_work._registry


def current_home(home=None):
    """Explicit home wins, then the active scope's, then the context-bound home key."""
    if home is not None:
        if isinstance(home, (str, os.PathLike)) and not str(home).startswith("unresolved-"):
            from hermes_constants import hermes_home_key
            return hermes_home_key(home)
        return home
    captured = ACTIVE_SCOPE.get()
    if captured is not None and captured.home is not None:
        return captured.home
    from hermes_constants import hermes_home_key
    return hermes_home_key()


def _resolve(kind, source, home):
    registry = installed_registry()
    if registry is None:
        return None
    try:
        home = current_home(home)
    except Exception:  # health: allow BLE001 -- home resolution failure latches unknown (fail closed) and the work stays counted
        registry.mark_unknown("home-unresolved")
        home = None
    return registry, home


class WorkHandle:
    """One reservation plus its captured scope; finish() is idempotent per handle."""

    __slots__ = ("_done", "_lock", "owner", "token")

    def __init__(self, registry, kind, home, source):
        self.owner = WorkScope(registry, kind, home, source)
        self.token = registry.reserve(kind, home=home, source=source)
        self._lock = threading.Lock()
        self._done = False

    def finish(self, _unused=None):
        with self._lock:
            if self._done:
                return False
            self._done = True
        return self.owner.registry.finish(self.token)


def begin_work(kind, source, home=None):
    """Reserve BEFORE scheduling/starting. Returns a WorkHandle or None (no registry)."""
    resolved = _resolve(kind, source, home)
    if resolved is None:
        return None
    registry, home = resolved
    return WorkHandle(registry, kind, home, source)


def end_work(handle):
    if handle is not None:
        handle.finish()


class _Nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class work_scope:
    """Inline scope: reserve on enter, release at exit; sets ACTIVE_SCOPE for children."""

    def __init__(self, kind, source, home=None):
        self._args = (kind, source, home)
        self._handle = None
        self._ctx = None

    def __enter__(self):
        self._handle = begin_work(*self._args)
        if self._handle is not None:
            self._ctx = ACTIVE_SCOPE.set(self._handle.owner)
        return self._handle

    def __exit__(self, *exc):
        try:
            if self._ctx is not None:
                ACTIVE_SCOPE.reset(self._ctx)
        finally:
            end_work(self._handle)
        return False


def create_tracked_task(create, kind, source, home=None):
    """Reserve, call ``create()`` (e.g. a lambda wrapping asyncio.create_task) with the
    scope bound so the task context inherits it, release at the task done-callback
    (fires for cancelled-before-start tasks too). create() failure releases once."""
    handle = begin_work(kind, source, home)
    if handle is None:
        return create()
    ctx = ACTIVE_SCOPE.set(handle.owner)
    try:
        task = create()
    except BaseException:
        handle.finish()
        raise
    finally:
        ACTIVE_SCOPE.reset(ctx)
    if hasattr(task, "add_done_callback"):
        task.add_done_callback(handle.finish)
    else:  # test sentinel: no physical task exists to outlive this call
        handle.finish()
    return task


_scoped_loops = weakref.WeakKeyDictionary()  # loop -> its scope-aware default executor


async def run_in_pool(executor, ctx, func, *args, source="gateway:run_in_executor"):
    """Gateway pool offload: reserve before submit; the pool thread releases at its own exit,
    so a cancelled or timed-out waiter (``wrap_future`` cancel) never stands in for the thread.
    Plain ``run_in_executor`` while no registry is installed."""
    loop = asyncio.get_running_loop()
    install_scope_default_executor(loop)
    registry = installed_registry()
    if registry is None:
        return await loop.run_in_executor(executor, ctx.run, func, *args)
    if executor is None:  # ``run_in_executor(None, ...)`` semantics: the loop's (scope-aware) default
        executor = _scoped_loops[loop]
    # This path submits directly, bypassing BaseEventLoop.run_in_executor's
    # accounting hook. Keep the submitted function named on its physical thread;
    # Scope subtracts any route/pass CPU the function already accounts itself.
    from hermes_cli import loop_accounting
    if loop_accounting.scopes_enabled():
        func = loop_accounting.scoped_call("thread", func, args)
    future = submit_tracked(executor, ctx.run, func, *args, registry=registry,
                            home=current_home(), source=source, kind="gateway_pool")
    return await asyncio.wrap_future(future, loop=loop)


def install_scope_default_executor(loop):
    """Idempotently make ``loop``'s default executor scope-aware (explicit, per loop; never
    patches asyncio globally). Passive unless an active scope exists at submit time, so
    maintenance ``to_thread`` stays uncounted while scoped work keeps physical custody."""
    if loop in _scoped_loops:
        return False
    executor = ScopeAwareExecutor(thread_name_prefix="hermes-gateway-default")
    loop.set_default_executor(executor)
    _scoped_loops[loop] = executor
    return True
