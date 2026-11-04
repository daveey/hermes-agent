"""Prepare desktop terminal consent without running shells ahead of their turn.

Workers keep execution middleware on its original stack. Only command approval
runs ahead; the existing sequential executor releases each worker and persists
its result before releasing the next. No terminal environment/cwd is acquired
while preparing, and the real execution still runs every command guard.
"""
from __future__ import annotations

import contextvars
import copy
import threading
import time
from contextlib import contextmanager
from typing import Any

from tools.thread_context import propagate_context_to_thread

_batch: contextvars.ContextVar[Any] = contextvars.ContextVar("terminal_approval_batch", default=None)
_slot: contextvars.ContextVar[Any] = contextvars.ContextVar("terminal_approval_slot", default=None)


class _CancelledPreparation(Exception):
    pass


class _TerminalSlot:
    def __init__(self, batch, parsed, index):
        self.batch, self.parsed, self.index = batch, parsed, index
        self.ready = threading.Event()
        self.release = threading.Event()
        self.future: Any = None
        self.tids = []
        self.preparing = False
        self.args = None
        self.decision = None
        self.guard_key = None
        self.claimed = False

    def check_cancelled(self):
        if self.batch.cancelled.is_set() or self.batch.agent._interrupt_requested:
            raise _CancelledPreparation("Terminal approval preparation cancelled; command was not started")

    def prepare(self, ref):
        from tools import terminal_tool as tt
        self.check_cancelled()
        self.args = copy.deepcopy(ref.args)
        self.preparing = True
        try:
            # Read policy only. _plan_execution/_acquire_env resolve cwd and
            # shell state later, after the previous result has been persisted.
            config = tt._get_env_config()
            if isinstance(ref.args.get("command"), str):
                from tools.approval_context import set_current_observability_context, reset_current_observability_context
                tokens = set_current_observability_context(
                    tool_call_id=ref.call_id, session_id=self.batch.agent.session_id or "",
                    turn_id=getattr(self.batch.agent, "_current_turn_id", "") or "",
                )
                try:
                    # Key on what will actually run: a foreground `cmd &` executes as `cmd`.
                    command = ref.args["command"]
                    if not ref.args.get("background"):
                        command = tt.trailing_amp_background_command(command) or command
                    self.guard_key = (command, config["env_type"], tt._docker_has_host_access(config))
                    self.decision = tt._check_all_guards(*self.guard_key)
                finally:
                    reset_current_observability_context(tokens)
        finally:
            self.preparing = False
            self.ready.set()
        while not self.release.wait(0.1):
            self.check_cancelled()
        self.check_cancelled()

    def run(self):
        from agent import tool_executor as te
        token = _slot.set(self)
        pc, batch = self.parsed, self.batch
        ref = pc.ref(batch.task_id)
        try:
            with te._registered_tool_worker(batch.agent) as tid:
                self.tids.append(tid)
                self.check_cancelled()
                dispatch = te._resolve_sequential_dispatch(batch.agent, ref, batch.messages)
                return te._run_agent_tool_execution_middleware(
                    batch.agent, **ref.middleware_kwargs(), execute=dispatch.execute,
                    scope_block=pc.scope_block, display_index=self.index + 1,
                    authorization_gate=batch.authorization_gate,
                )
        finally:
            self.ready.set()
            _slot.reset(token)


class _TerminalBatch:
    def __init__(self, agent, messages, task_id, parsed):
        from agent.tool_executor import _ConcurrentToolAuthorizationGate
        from tools.daemon_pool import DaemonThreadPoolExecutor
        self.agent, self.messages, self.task_id = agent, messages, task_id
        self.cancelled = threading.Event()
        self.pending_approvals = []  # guarded by tools.approval._lock
        self.authorization_gate = _ConcurrentToolAuthorizationGate()
        self.executor = DaemonThreadPoolExecutor(max_workers=len(parsed))
        self.slots = [_TerminalSlot(self, pc, i) for i, pc in enumerate(parsed)]
        # Set once ANY slot in the batch has published a failed result (or a
        # denied/blocked one). Informed consent is per the batch state the user
        # SAW: after a failure, a later slot's pre-collected approval no longer
        # describes the world its command will run in, so it must not be
        # consumed — the guard re-runs live instead (#113158).
        self.failure_seen = False
        # Set when the batch stopped itself (a prepared command or the preparation timed out).
        # The sequential loop then reports every unstarted call with this notice and the turn
        # goes on: the model gets the error, as it does outside a batch. Aborting the turn here
        # ended it with no reply at all (self-heal plugin-9605e451d0).
        self.stop_notice: str | None = None

    def stop(self, notice: str):
        """Close the batch so no later prepared command starts; later calls get ``notice``."""
        self.stop_notice = notice
        self.close()

    def start(self):
        from agent.tool_executor import _resolve_sequential_tool_timeout
        for slot in self.slots:
            slot.check_cancelled()
            slot.future = self.executor.submit(propagate_context_to_thread(slot.run))
            timeout = _resolve_sequential_tool_timeout()
            started = time.monotonic()
            baseline = self.authorization_gate.excluded_seconds()
            # Proceed once the worker publishes a human request OR completes
            # preparation. A wedged plugin must not hold the batch forever.
            while not slot.ready.wait(0.1):
                slot.check_cancelled()
                elapsed = time.monotonic() - started - (self.authorization_gate.excluded_seconds() - baseline)
                if timeout is not None and elapsed >= timeout:
                    raise TimeoutError("Terminal approval preparation timed out; commands were not started")

    def close(self):
        from agent.tool_executor import _interrupt_worker_tids
        from tools import approval
        # Withdraw only this batch's requests, including a worker wedged in
        # notify_cb. Thread interrupts alone leave those requests actionable.
        with approval._lock:
            self.cancelled.set()
            for session_key, entry in self.pending_approvals:
                queue = approval._gateway_queues.get(session_key, [])
                if entry in queue:
                    queue.remove(entry)
                    entry.result = "deny"
                    entry.event.set()
                if not queue:
                    approval._gateway_queues.pop(session_key, None)
            self.pending_approvals.clear()
        for slot in self.slots:
            slot.release.set()
            if slot.future is not None and not slot.future.done():
                _interrupt_worker_tids(self.agent, slot.tids)
                slot.future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)


def prepare_current_terminal(ref):
    slot = _slot.get()
    if slot is not None and ref.name == "terminal":
        slot.prepare(ref)


def bind_prepared_dispatch(dispatch):
    """A middleware-owned thread must not lose the batch's execution barrier."""
    slot = _slot.get()
    if slot is None:
        return dispatch
    from agent.tool_executor import _registered_tool_worker

    owner_tid = threading.get_ident()

    def tracked(*args, **kwargs):
        if threading.get_ident() == owner_tid:
            return dispatch(*args, **kwargs)
        with _registered_tool_worker(slot.batch.agent) as tid:
            slot.tids.append(tid)
            slot.check_cancelled()
            return dispatch(*args, **kwargs)

    invoke = propagate_context_to_thread(tracked)
    # Each batch slot has exactly one dispatch. Reject concurrent/replayed
    # continuations before entering its captured Context on another thread.
    lock = threading.Lock()
    claimed = False

    def once(*args, **kwargs):
        nonlocal claimed
        with lock:
            if claimed:
                raise RuntimeError("Hermes tool execution callback invoked more than once")
            claimed = True
        return invoke(*args, **kwargs)

    return once


COMMAND_TIMEOUT_NOTICE = "[Not run: {name} was not started because an earlier terminal command in this batch timed out]"
PREPARATION_TIMEOUT_NOTICE = "[Not run: {name} was not started because terminal approval preparation for this batch timed out]"


def batch_stop_notice():
    """The current batch's stop notice once it stopped itself, else None."""
    batch = _batch.get()
    return None if batch is None else batch.stop_notice


def report_batch_stop(agent, messages, remaining, task_id):
    """None while the current batch runs. Once it stopped itself, append one not-run row per
    ``remaining`` call and return True (False when a flush failed: the caller stops the batch)."""
    notice = batch_stop_notice()
    if notice is None:
        return None
    from agent.tool_executor import _append_skipped_tool_results
    agent._vprint(f"{agent.log_prefix}⏱ Terminal batch stopped: skipping {len(remaining)} call(s)", force=True)
    return _append_skipped_tool_results(agent, messages, remaining, task_id, content=notice,
                                        flush_stage="skipped tool result")


def take_prepared_call(call_id):
    batch = _batch.get()
    if batch is None:
        return None
    for slot in batch.slots:
        if slot.parsed.ref(batch.task_id).call_id == call_id and not slot.claimed:
            slot.claimed = True
            slot.check_cancelled()
            slot.release.set()
            return slot
    return None


def approval_published():
    slot = _slot.get()
    if slot is not None:
        slot.ready.set()


def register_prepared_approval(session_key, entry):
    """Called under the approval queue lock, before enqueueing the request."""
    slot = _slot.get()
    if slot is not None:
        slot.check_cancelled()
        slot.batch.pending_approvals.append((session_key, entry))


def consume_prepared_guard(command, env_type, has_host_access):
    slot = _slot.get()
    if slot is None or slot.preparing:
        return None
    slot.check_cancelled()
    # Re-gate after an earlier slot in the same batch failed (#113158): the
    # user approved a batch where every command was expected to run; once one
    # failed, that informed consent is stale for the commands after it, so
    # drop the pre-made decision and let the guard run its live flow
    # (allowlist, human approval). Nothing is auto-denied: an explicit
    # human answer still wins; the prepared (often auto/policy) decision is
    # simply not consumed.
    if slot.batch.failure_seen and slot.decision is not None:
        slot.decision = None
        return None
    from tools.approval_context import _approval_tool_call_id
    if (_approval_tool_call_id.get() != slot.parsed.ref(slot.batch.task_id).call_id
            or slot.guard_key != (command, env_type, has_host_access)):
        return None
    decision, slot.decision = slot.decision, None  # single-use, even for identical calls
    # Batching exists to publish the human asks together. An approval nobody answered (/yolo,
    # approvals.mode off, the allowlist, a clean command) is policy, and the policy in force NOW
    # governs: switching YOLO or "Approvals: off" off mid-batch must stop the later commands.
    if decision is not None and decision.get("approved") and not decision.get("user_approved"):
        from tools.approval_context import _get_approval_mode
        if not (decision.get("smart_approved") and _get_approval_mode() == "smart"):
            return None
    return decision


def preparing_terminal_approval():
    slot = _slot.get()
    return slot is not None and slot.preparing


def mark_batch_outcome(failed: bool) -> None:
    """Record that the batch's current slot published a failed result.

    Called by the sequential publisher AFTER a result is committed, so the
    flag lands only for failures the model actually sees (a wedged worker's
    late result never publishes). Sticky for the batch: one failure re-gates
    every later prepared slot (#113158); successes leave it alone — a later
    success must not un-stale an approval after an even earlier failure.
    """
    batch = _batch.get()
    if batch is not None and failed:
        batch.failure_seen = True


def validate_prepared_terminal(args):
    slot = _slot.get()
    if slot is not None:
        slot.check_cancelled()
        # Middleware/registry coercion must not turn a prepared consent into
        # authority for different arguments, even with identical display text.
        if args != slot.args:
            slot.decision = None
            raise RuntimeError("Terminal arguments changed after approval preparation; command was not started")


def terminal_approval_runs(agent, calls):
    """Keep nonterminal barriers, but batch adjacent terminals in mixed segments."""
    from itertools import groupby
    from agent.tool_executor import _parse_tool_call

    def is_terminal(call):
        pc = _parse_tool_call(agent, call, flatten_probe=True)
        return pc.name == "terminal" and pc.parse_error is None

    for _, run in groupby(calls, key=is_terminal):
        yield list(run)


@contextmanager
def terminal_approval_batch(agent, calls, messages, task_id):
    from gateway.session_context import get_session_env
    from tools import approval
    from agent.tool_executor import _parse_tool_call
    if (len(calls) < 2 or get_session_env("HERMES_SESSION_SOURCE") != "desktop"
            or approval._gateway_notify_cb(approval.get_current_session_key()) is None):
        yield
        return
    parsed = [_parse_tool_call(agent, call, flatten_probe=True) for call in calls]
    # Never prepare across a nonterminal barrier; leave mixed segments and
    # malformed calls with the established sequential path.
    ids = [pc.ref(task_id).call_id for pc in parsed]
    if (any(pc.name != "terminal" or pc.parse_error is not None for pc in parsed)
            or not all(ids) or len(set(ids)) != len(ids)):
        yield
        return
    batch = _TerminalBatch(agent, messages, task_id, parsed)
    token = _batch.set(batch)
    try:
        if not agent._interrupt_requested and not getattr(agent, "_incremental_persistence_failed", False):
            try:
                batch.start()
            except (_CancelledPreparation, TimeoutError) as exc:
                # A cancellation means a stop is already published (or our own
                # close() ran): re-interrupting would overwrite the user's queued
                # message/redirect. A timeout stops only the batch, never the turn:
                # every call is reported as not run and the model answers.
                if isinstance(exc, TimeoutError):
                    batch.stop(PREPARATION_TIMEOUT_NOTICE)
                else:
                    batch.close()
                # The sequential path must still persist a result for every
                # assistant tool call, even if preparation never finished.
        yield
    finally:
        batch.close()
        _batch.reset(token)
