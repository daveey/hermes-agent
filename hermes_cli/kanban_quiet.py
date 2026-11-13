"""``hermes kanban quiet-hold --max-s N --reason TEXT -- OBSERVER_COMMAND...``

Takes the root-scoped claim-quiet lease (:mod:`agent.claim_quiet`), runs the observer command under
it and releases the lease on every path. Root-scoped: it never opens or initialises a Kanban board
and never writes configuration; ``--board`` has no effect here.

Lease facts: at most 480 s, no renewal, a 2 h cooldown measured from the START of the previous
episode (consumed even when the observer then fails to spawn), admission open the instant the
producer dies, releases or the deadline passes. Nothing this command does after the deadline (or
during cleanup) can extend suppression: the lease is released *before* the observer is signalled.

Observer custody: argv is executed without a shell in a NEW session/process group with
``close_fds=True`` (the lease descriptors are never inherited). This process owns the deadline:
at the lease deadline, on SIGINT/SIGTERM/SIGHUP, the lease is released, the group is sent TERM,
given a bounded grace, sent KILL, the immediate child is reaped, and group death is verified. A
group that cannot be shown dead is reported (exit 125) and never reported as success.

Lease identity is passed to the observer through the environment:
``HERMES_CLAIM_QUIET_EPISODE_ID``, ``HERMES_CLAIM_QUIET_PRODUCER_PID``,
``HERMES_CLAIM_QUIET_EXPIRES_AT`` (UTC epoch seconds), ``HERMES_CLAIM_QUIET_MAX_S``.

Exit codes: 0 observer succeeded; 2 usage; 69 lease unavailable (platform/filesystem/clock/record);
70 observer failed (spawn failure or non-zero exit; the observer's own status is on stderr);
71 lease release failed; 75 busy (another lease is active); 76 cooldown; 78 preflight refused;
124 deadline reached and the observer was terminated; 125 observer group death could not be
verified; 130 cancelled by signal.

Preflight (exit 78): before the lease is taken, so before the cooldown is committed, the host
files the gateway's ``quiet-observe-start`` refuses on are read (``gateway.quiet_control.
start_preflight_reasons``): a core-update drain/admission hold, and the deploy window, which must
be this host's, ``idle`` and at most ``UPDATE_WINDOW_MAX_AGE_S - START_PREFLIGHT_MARGIN_S`` old.
Nothing is written; retry when the window is fresh and idle. Runner-only refusals (attestation,
coverage pending, unknowns) have no read-only control verb, so they still surface only from the
gateway's start, after the lease. Once a start is attempted, lease and cooldown are unchanged.

Proof targets: Board pool workers republish ``window.json`` only from the 10-minute
``hermes-core-update.timer`` and batch updates, so their window is usually stale or ``batching``
for the gateway's 120 s bound. Workers are not quiet-proof targets (the live quiet proof is owned on
the Macs); on a worker this command normally exits 78 at no cost instead of spending a 2 h cooldown.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 69
EXIT_OBSERVER_FAILED = 70
EXIT_RELEASE_FAILED = 71
EXIT_BUSY = 75
EXIT_COOLDOWN = 76
EXIT_PREFLIGHT = 78
EXIT_DEADLINE = 124
EXIT_UNRESOLVED = 125
EXIT_CANCELLED = 130

TERM_GRACE_S = 5.0
KILL_WAIT_S = 5.0
_POLL_S = 0.05
_SIGNALS = tuple(s for s in (getattr(signal, n, None) for n in ("SIGINT", "SIGTERM", "SIGHUP")) if s)

ENV_EPISODE = "HERMES_CLAIM_QUIET_EPISODE_ID"
ENV_PRODUCER_PID = "HERMES_CLAIM_QUIET_PRODUCER_PID"
ENV_EXPIRES_AT = "HERMES_CLAIM_QUIET_EXPIRES_AT"
ENV_MAX_S = "HERMES_CLAIM_QUIET_MAX_S"
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _err(message: str) -> None:
    print(f"quiet-hold: {message}", file=sys.stderr, flush=True)


def _pause(seconds: float) -> None:
    threading.Event().wait(seconds)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)  # windows-footgun: ok -- quiet-hold is POSIX-only (os.waitid, process groups)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours to signal: cannot be certified dead
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)  # windows-footgun: ok -- quiet-hold is POSIX-only (os.waitid, process groups)
    except ProcessLookupError:
        pass


def _leader_exited(pid: int) -> bool:
    """True once the immediate child has exited, WITHOUT reaping it (a zombie leader keeps the
    process-group id from being recycled while we are still signalling the group)."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _terminate_group(proc: subprocess.Popen) -> bool:
    """TERM, bounded grace, KILL, reap the child, verify the group is gone. True when verified."""
    pgid = proc.pid  # start_new_session made the child its own group leader
    _signal_group(pgid, signal.SIGTERM)
    grace_end = time.monotonic() + TERM_GRACE_S
    while time.monotonic() < grace_end and not _leader_exited(proc.pid):
        _pause(_POLL_S)
    _signal_group(pgid, _SIGKILL)  # always: the leader exiting does not end its descendants
    try:
        proc.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        return False
    end = time.monotonic() + KILL_WAIT_S
    while _group_alive(pgid):
        if time.monotonic() >= end:
            return False
        _signal_group(pgid, _SIGKILL)
        _pause(_POLL_S)
    return True


def _remaining_s(lease, clock) -> float:
    """Time left on the lease by BOTH its suspend-inclusive and wall deadlines (the smaller wins)."""
    return min(lease.remaining_s(clock.elapsed_ns()), max(0.0, lease.expires_at - clock.wall()))


def _observer_env(lease) -> dict[str, str]:
    env = dict(os.environ)  # health: allow PS-P05 -- the observer measures THIS profile and must see its env
    env.update({ENV_EPISODE: lease.episode_id, ENV_PRODUCER_PID: str(lease.producer_pid),
                ENV_EXPIRES_AT: repr(lease.expires_at), ENV_MAX_S: str(lease.max_s)})
    return env


def _release(owner) -> bool:
    try:
        owner.close()
    except Exception as exc:  # health: allow BLE001 -- the failure is reported, never swallowed
        _err(f"lease release failed (admission still opens at process exit): {exc!r}")
        return False
    return True


def _preflight() -> list[str]:
    """Lease-free reasons the gateway's ``quiet-observe-start`` would refuse; never raises."""
    try:
        from gateway.quiet_control import start_preflight_reasons
        return start_preflight_reasons()
    except Exception as exc:  # health: allow BLE001 -- an unreadable preflight refuses, never passes
        return [f"preflight_unreadable:{type(exc).__name__}"]


def hold(max_s: int, reason: str, argv: Sequence[str], *, clock=None) -> int:
    """Acquire the lease, run ``argv`` under it, release in ``finally``; returns the exit code."""
    from agent import claim_quiet as cq
    from agent.claim_quiet_records import SystemClock

    refused = _preflight()
    if refused:
        _err("gateway quiet start would refuse (no lease taken, no cooldown spent): "
             + ", ".join(refused))
        return EXIT_PREFLIGHT
    clock = clock or SystemClock()
    try:
        owner = cq.acquire(max_s=max_s, reason=reason, clock=clock)
    except cq.QuietCooldownError as exc:
        _err(str(exc))
        return EXIT_COOLDOWN
    except cq.QuietBusyError as exc:
        _err(str(exc))
        return EXIT_BUSY
    except (cq.QuietUnavailableError, cq.QuietLeaseError) as exc:
        _err(f"unavailable: {exc}")
        return EXIT_UNAVAILABLE

    cancelled: list[int] = []
    previous: dict[int, object] = {}
    released = [False]
    procs: list[subprocess.Popen] = []  # filled the instant Popen returns, read by the finally
    try:
        lease = owner.lease
        assert lease is not None  # acquire() publishes before it returns
        if threading.current_thread() is threading.main_thread():
            for sig in _SIGNALS:  # handlers only record: no exception can unwind custody
                previous[sig] = signal.signal(sig, lambda signum, _f: cancelled.append(signum))
        _err(f"lease {lease.episode_id} held for at most {lease.max_s}s")
        return _run_observer(owner, lease, argv, clock, cancelled, released, procs)
    finally:
        try:
            if not released[0]:
                _release(owner)
            if procs and procs[0].returncode is None:  # an exception path: never leak the group
                _terminate_group(procs[0])
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)  # type: ignore[arg-type]


def _run_observer(owner, lease, argv, clock, cancelled, released, procs) -> int:
    try:
        proc = subprocess.Popen(list(argv), shell=False, start_new_session=True,  # detached-spawn: ok — awaited by the lease deadline loop, whose settle kills the whole group; health: allow HX006 -- the lease deadline loop below owns the timeout
                                close_fds=True, env=_observer_env(lease))
    except (OSError, ValueError) as exc:
        _err(f"cannot start observer {argv[0]!r}: {exc}")
        return EXIT_OBSERVER_FAILED  # cooldown stays consumed; hold()'s finally releases the lease
    procs.append(proc)
    outcome = None
    while outcome is None:
        if cancelled:
            outcome = "cancelled"
        elif _remaining_s(lease, clock) <= 0:
            outcome = "deadline"
        elif _leader_exited(proc.pid):
            outcome = "exited"
        else:
            _pause(min(_POLL_S, max(0.001, _remaining_s(lease, clock))))
    released[0] = _release(owner)  # admission opens BEFORE any signalling; cleanup cannot extend it
    code = _settle(proc, outcome)
    return EXIT_RELEASE_FAILED if code == EXIT_OK and not released[0] else code


def _settle(proc: subprocess.Popen, outcome: str) -> int:
    if outcome == "exited":
        try:
            status = proc.wait(timeout=KILL_WAIT_S)  # the leader already exited (WNOWAIT probe)
        except subprocess.TimeoutExpired:
            _err("observer leader exit could not be reaped")
            return EXIT_UNRESOLVED
        if _group_alive(proc.pid) and not _kill_leftovers(proc.pid):
            _err("observer left descendants that could not be verified dead")
            return EXIT_UNRESOLVED
        if status != 0:
            _err(f"observer exited with status {status}")
        return EXIT_OK if status == 0 else EXIT_OBSERVER_FAILED
    if not _terminate_group(proc):
        _err("observer process group could not be verified dead")
        return EXIT_UNRESOLVED
    if outcome == "deadline":
        _err("lease deadline reached; observer terminated, episode invalid")
        return EXIT_DEADLINE
    _err("cancelled; observer terminated, episode invalid")
    return EXIT_CANCELLED


def _kill_leftovers(pgid: int) -> bool:
    end = time.monotonic() + KILL_WAIT_S
    while _group_alive(pgid):
        if time.monotonic() >= end:
            return False
        _signal_group(pgid, _SIGKILL)
        _pause(_POLL_S)
    return True


def cmd_quiet_hold(args) -> int:
    """Entry point from the ``hermes kanban`` parser."""
    argv = list(getattr(args, "observer_argv", None) or [])
    if not argv or argv[0] != "--":
        _err("usage: hermes kanban quiet-hold --max-s N --reason TEXT -- OBSERVER_COMMAND...")
        return EXIT_USAGE
    argv = argv[1:]
    if not argv or not argv[0]:
        _err("an observer command is required after '--'")
        return EXIT_USAGE
    from agent import claim_quiet as cq

    try:
        max_s, reason = cq._validate_request(getattr(args, "max_s", None), getattr(args, "reason", None))
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    return hold(max_s, reason, argv)


def entry(args) -> int:
    """Parser ``func``: lets quiet-hold bypass the board-resolving ``kanban_command`` entirely."""
    return cmd_quiet_hold(args)
