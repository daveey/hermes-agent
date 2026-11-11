"""[daveey] Worker-thread gates for tool batches, split out of ``agent.tool_executor`` to keep it under
the FILE_LINES cap (t_01a1c895eb8f4f8f). The code is moved verbatim and re-exported from
``agent.tool_executor``, which stays the import path for callers and tests.

Holds the authorization gate (serialized approval prompts + human-wait deadline exclusion), the
start-order gate, and worker-tid registration / interrupt fan-out. ``run_agent`` is reached lazily via
``_ra()`` so ``run_agent._set_interrupt`` patches still work.
"""

from __future__ import annotations

import contextlib
import logging
import threading

# Same logger as before the split: record names and per-logger levels are unchanged.
logger = logging.getLogger("agent.tool_executor")


def _ra():
    """Lazy reference to ``run_agent`` so patches like ``run_agent._set_interrupt`` work."""
    import run_agent
    return run_agent


# Fallback only; the effective bound derives from approvals.timeout (_authorization_gate_lock_timeout).
_AUTHORIZATION_GATE_LOCK_TIMEOUT_S = 360.0


def _authorization_gate_lock_timeout() -> float:
    """Authorization-lock bound = ``tools.approval_human_wait.human_wait_ceiling`` (approval timeout +
    margin, capped so it can't overflow Lock.acquire): never break serialization while a
    prompt is answerable, never let a wedged holder park workers forever. Deliberately NOT
    min()'d with the fallback so the gate never gives up early.

    Delegates to ``tools.approval_human_wait.human_wait_ceiling`` — the same bound that clamps a human-wait window's
    deadline contribution — so the two can't drift. Long enough that serialization is never broken while a
    legitimate approval prompt is still answerable; short enough that a wedged holder (hanging
    ``pre_tool_call`` plugin, dead approval client) cannot park other workers forever (#79719). Resolved
    once per gate (per batch), so a mid-process ``approvals.timeout`` change applies from the next batch.
    """
    try:
        from tools.approval_human_wait import human_wait_ceiling

        # human_wait_ceiling is platform-safety-capped (agent/deadline.py MAX_SAFE_TIMEOUT_S): a huge
        # approvals.timeout can no longer overflow Lock.acquire's time_t on macOS (#83220). Deliberately NOT
        # min()'d with _AUTHORIZATION_GATE_LOCK_TIMEOUT_S — the gate must never give up while a legitimate
        # approval prompt is still answerable (#79719), so a configured approvals.timeout above 360s must
        # extend the gate.
        return human_wait_ceiling()
    except Exception:
        return _AUTHORIZATION_GATE_LOCK_TIMEOUT_S


class _BatchAbandoned(BaseException):
    """Raised inside a worker when the batch was abandoned before dispatch; a BaseException
    so ``except Exception`` handlers in the middleware chain can't swallow it."""


class _ConcurrentToolAuthorizationGate:
    """Serialize policy prompts and exclude human approval waits from batch deadlines.

    The acquire is BOUNDED: on expiry the worker prompts unserialized rather than starving
    the batch behind a wedged plugin/approval client. Exclusion is measured at the SOURCE
    of the human wait (``tools.approval.human_wait_seconds``), NOT as gate residency —
    residency-based exclusion let a wedged plugin keep the deadline from ever firing.

    Serialization keeps concurrent approval prompts from interleaving on the user's screen. The acquire is
    BOUNDED: a worker wedged inside the gate (a hanging ``pre_tool_call`` plugin, or an approval round-trip
    to a client that went away) must not park every other worker forever. On expiry the worker runs its
    prompt unserialized — worst case is interleaved prompts, strictly better than permanent starvation (same
    tradeoff as the start-order gate, #79705).
    Gate residency is arbitrary code — using it as the exclusion signal let a wedged plugin grow the
    exclusion 1:1 with wall clock, keeping the batch deadline's ``remaining`` constant so it never fired and
    the turn hung forever (#79719). A wedged plugin now contributes nothing to the exclusion and the batch
    times out normally, while a genuine approval wait (which can legitimately exceed any fixed bound) is
    still excluded in full.
    """

    def __init__(self, *, lock_timeout: float | None = None, session_key: str | None = None) -> None:
        self._serialization_lock = threading.Lock()
        self._lock_timeout = _authorization_gate_lock_timeout() if lock_timeout is None else lock_timeout
        self._session_key = session_key
        if self._session_key is None:
            # Snapshot on the SUBMITTING thread: excluded_seconds() is polled from the
            # batch wait loop, whose context may differ from the workers'.
            try:
                from tools.approval_context import get_current_session_key

                self._session_key = get_current_session_key()
            except Exception:
                logger.debug(
                    "authorization gate could not snapshot the session key; "
                    "human-wait exclusion will re-resolve it at poll time",
                    exc_info=True,
                )
        self._baseline_wait_seconds = self._human_wait_seconds()

    def _human_wait_seconds(self) -> float:
        try:
            from tools.approval_human_wait import human_wait_seconds

            return human_wait_seconds(self._session_key)
        except Exception:
            return 0.0

    def run(self, callback):
        if not self._serialization_lock.acquire(timeout=self._lock_timeout):
            # Deterministic failure (bad command, non-MCP URL, 401/403): every retry hits the same wall.
            # Park immediately instead of burning the retry ladder and spamming N identical warnings
            # (#65673). Auth failures park here too rather than returning. Returning ends the run task, and
            # with it the only listener on ``_reconnect_event`` — so a 401 on the very first connect left
            # the server unrevivable for the life of the process, even after the user re-authenticated with
            # ``hermes mcp login``. Parking keeps the task alive so the 300s self-probe (and an explicit
            # /mcp refresh) can pick up fresh tokens.
            logger.warning(
                "authorization gate lock not acquired after %.1fs "
                "(holder wedged in a pre_tool_call plugin or approval "
                "round-trip?); running prompt unserialized",
                self._lock_timeout,
            )
            return callback()
        try:
            return callback()
        finally:
            self._serialization_lock.release()

    def excluded_seconds(self) -> float:
        """Return human-approval wait seconds accrued since the batch started."""
        return max(0.0, self._human_wait_seconds() - self._baseline_wait_seconds)


@contextlib.contextmanager
def _registered_tool_worker(agent):
    """Track this worker tid for interrupt fan-out (``AIAgent.interrupt()``); on ANY exit
    (incl. BaseException) discard it and clear its interrupt bit so a recycled tid starts clean."""
    tid = threading.current_thread().ident
    with agent._tool_worker_threads_lock:
        agent._tool_worker_threads.add(tid)
    try:
        yield tid
    finally:
        with agent._tool_worker_threads_lock:
            agent._tool_worker_threads.discard(tid)
        with contextlib.suppress(Exception):
            _ra()._set_interrupt(False, tid)


_NO_REASON = object()


def _interrupt_worker_tids(agent, tids, *, reason=_NO_REASON) -> None:
    """Raise the interrupt bit on each worker tid (best-effort, via ``run_agent``)."""
    kwargs = {} if reason is _NO_REASON else {"reason": reason}
    for tid in tids:
        with contextlib.suppress(Exception):
            _ra()._set_interrupt(True, tid, **kwargs)


def _set_worker_activity_callback(agent) -> None:
    """The activity callback is thread-local: bind it on THIS thread so tool-layer heartbeats fire."""
    with contextlib.suppress(Exception):
        from tools.environments.base import set_activity_callback

        set_activity_callback(agent._touch_activity)


class _StartOrderGate:
    """Serialize worker dispatch by submit order (prompts appear in call order); ``abandon()``
    releases every parked worker so none dispatches a tool the turn already gave up on."""

    def __init__(self, timeout: float) -> None:
        self._condition = threading.Condition()
        self._next_order = 0
        self._timeout = timeout
        self.abandoned = threading.Event()

    def abandon(self) -> None:
        self.abandoned.set()
        with self._condition:
            self._condition.notify_all()

    def begin_in_order(self, order: int, callback=None, *, tool_name: str = "") -> bool:
        """Wait for ``order``, run ``callback``, advance. Returns False if abandoned."""
        with self._condition:
            # Bounded wait so one wedged dispatch can't starve later-ordered workers; on
            # expiry proceed out of order (interleaved prompts beat starvation). ``>=`` (not
            # ``==``) releases every skipped worker at once; abandoned short-circuits.
            in_order = self._condition.wait_for(
                lambda: self._next_order >= order or self.abandoned.is_set(), timeout=self._timeout,
            )
            if self.abandoned.is_set():
                return False  # the turn already synthesized this result; don't advance
            if not in_order:
                logger.warning(
                    "start-order gate timed out for %s (order=%d next=%d); proceeding out of order",
                    tool_name or "tool", order, self._next_order,
                )
            try:
                if callback is not None:
                    callback()
            finally:
                self._next_order = max(self._next_order, order + 1)
                self._condition.notify_all()
        return True


class _WorkerStartOnce:
    """One worker's handle on the start-order gate: advances at most once, raising
    ``_BatchAbandoned`` (instead of dispatching late) when the batch was abandoned."""

    def __init__(self, gate: _StartOrderGate, order: int, tool_name: str) -> None:
        self._gate, self._order, self._tool_name, self._advanced = gate, order, tool_name, False

    def advance(self, callback=None) -> None:
        if self._advanced:
            return
        self._advanced = True
        if not self._gate.begin_in_order(self._order, callback, tool_name=self._tool_name):
            raise _BatchAbandoned(self._tool_name)
