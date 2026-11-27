"""Supported POSIX no-apply deadline protocol; never cancel committed update custody.

A sync caller carries an absolute CLOCK_MONOTONIC deadline in DEADLINE_ENV. The
command supervises a dedicated child until that child asks to enter its first
checkout/custody gate. Only the supervisor can acknowledge that transition, and
it relinquishes cancellation BEFORE sending the acknowledgement. Nothing here
removes admission holds, resumes turns, or cancels an already-committed install.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

PROTOCOL_VERSION = 1
DEADLINE_ENV = "HERMES_UPDATE_PRECOMMIT_DEADLINE_V1"
REFUSED_EXIT = 85
REFUSAL = "precommit deadline refused; checkout not changed"
CANCEL_SETTLE_SECONDS = 5.0  # reserve reaping time INSIDE the caller's absolute release deadline
_child_channel: socket.socket | None = None
_child_deadline: float | None = None
_committed = False
_wrote = False


def clock() -> float:
    """Same host/boot clock as hermes-sync._mono, including its macOS semantics."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def parse_deadline(raw: str) -> float:
    """Version/clock are part of the carrier; NaN, bool and foreign clocks refuse."""
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"version", "clock", "deadline"}:
        raise ValueError("invalid precommit deadline carrier")
    deadline = data["deadline"]
    if (type(data["version"]) is not int or data["version"] != PROTOCOL_VERSION
            or data["clock"] != "CLOCK_MONOTONIC" or type(deadline) not in (int, float)
            or not math.isfinite(deadline) or deadline <= 0):
        raise ValueError("invalid precommit deadline version, clock or value")
    return float(deadline)


def preparing() -> bool:
    return _child_channel is not None and not _committed


def deferred_normalization(git_cmd, root: Path, checkout_move) -> None:
    """Keep ordinary churn handling, but only after fetch/pinned-target validation."""
    if preparing():
        from hermes_cli.update_cmd import _discard_lockfile_churn, _normalize_managed_eol
        _discard_lockfile_churn(git_cmd, root, checkout_move=checkout_move)
        _normalize_managed_eol(git_cmd, root, checkout_move=checkout_move)


def commit_gate() -> None:
    """First custody boundary: no checkout writer may run before the ACK.

    A lost/late ACK fails closed. Once ACKed the supervisor NEVER deadline-kills
    the child, even if gateway stop/build/recovery takes longer than the budget.
    """
    global _committed
    if not preparing():
        return
    if clock() >= _child_deadline:
        raise SystemExit(REFUSED_EXIT)
    _child_channel.settimeout(max(0.001, _child_deadline - clock()))
    try:
        _child_channel.sendall(b"C")
        ack = _child_channel.recv(1)
    except (OSError, TimeoutError) as exc:
        raise SystemExit(REFUSED_EXIT) from exc
    if ack != b"Y":
        raise SystemExit(REFUSED_EXIT)
    _committed = True
    _child_channel.close()


def before_checkout_write() -> None:
    """Recheck after slow pause/validation, before the FIRST actual tree write.

    The supervisor already handed off custody, so an orderly refusal can restore
    stopped gateways without a deadline kill. After a write, recovery must run
    regardless of expiry; no later step may abandon a partially installed tree.
    """
    global _wrote
    if _child_deadline is None or _wrote:
        return
    if clock() >= _child_deadline:
        print(f"↷ Update deferred: {REFUSAL}", flush=True)
        raise SystemExit(REFUSED_EXIT)
    _wrote = True


class _PreparationCancelled(BaseException):
    """A supervisor signal cancels preparation, never committed child custody."""


def _terminate_uncommitted(proc: subprocess.Popen) -> None:
    # Only our unreaped, start_new_session child, never a chat/gateway group.
    # Its Git children share the group on Linux; macOS owner-watchers settle
    # separately grouped Git children when this owner exits.
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok -- supervisor is POSIX-only
    except ProcessLookupError:
        pass
    proc.wait(timeout=CANCEL_SETTLE_SECONDS)


def supervise(command: list[str], deadline: float, env: dict[str, str]) -> int:
    """Run an isolated preparation child; after ACK use ordinary, unlimited custody."""
    cancel_at = deadline - CANCEL_SETTLE_SECONDS
    if clock() >= cancel_at:
        print(f"↷ Update deferred: {REFUSAL}", flush=True)
        return REFUSED_EXIT
    parent, child = socket.socketpair()
    with parent, child:
        # detached-spawn: ok -- awaited dedicated updater; only uncommitted preparation is cancellable
        proc = subprocess.Popen([*command, str(child.fileno()), repr(deadline)], env=env,
                                pass_fds=(child.fileno(),), start_new_session=True)
        child.close()
        committed = False
        cancelling = False
        def interrupted(signum, frame):
            nonlocal cancelling
            if not committed and not cancelling:
                cancelling = True
                raise _PreparationCancelled
            # Once custody is committed, termination cannot make the caller
            # release its inherited hold while the child is still installing.
        previous = {sig: signal.signal(sig, interrupted)
                    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}  # windows-footgun: ok -- POSIX protocol only
        try:
            while proc.poll() is None:
                remaining = cancel_at - clock()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([parent], [], [], min(remaining, 0.05))
                if ready:
                    request = parent.recv(1)
                    if request == b"C" and clock() < cancel_at:
                        # Set BEFORE send: an ACK whose delivery is uncertain must
                        # never be followed by a parent deadline kill.
                        committed = True
                        try:
                            parent.sendall(b"Y")
                        except OSError:
                            pass  # uncertain delivery retains custody until child exits
                        return proc.wait()  # health: allow HX006 -- committed custody must settle, never deadline-kill
                    if request:
                        break
                    # EOF is not a commitment. A child which closes the channel
                    # but stays alive is still cancellable at the original deadline.
                    return proc.wait(timeout=max(0.001, cancel_at - clock()))
            if proc.returncode is not None:
                return proc.returncode
        except (OSError, subprocess.TimeoutExpired, _PreparationCancelled):
            if committed:
                return proc.wait()  # health: allow HX006 -- observation failure cannot withdraw committed custody
        finally:
            cancelling = True
            try:
                if not committed and proc.poll() is None:
                    _terminate_uncommitted(proc)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        print(f"↷ Update deferred: {REFUSAL}", flush=True)
        return REFUSED_EXIT


def run_if_requested(args: argparse.Namespace, root: Path) -> bool:
    """Command entry. No carrier means the completely unchanged ordinary path."""
    raw = os.environ.get(DEADLINE_ENV)
    if raw is None:
        return False
    try:
        if sys.platform not in ("linux", "darwin"):
            raise ValueError("precommit deadline requires supported POSIX custody")
        deadline = parse_deadline(raw)
        payload = json.dumps({key: value for key, value in vars(args).items() if not callable(value)})
    except (ValueError, TypeError) as exc:
        print(f"↷ Update deferred: invalid precommit deadline ({exc})", flush=True)
        raise SystemExit(REFUSED_EXIT) from exc
    env = dict(os.environ)
    env.pop(DEADLINE_ENV)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from hermes_cli.update_precommit import child_entry; child_entry()")
    raise SystemExit(supervise([sys.executable, "-c", code, str(root), payload], deadline, env))


def child_entry() -> None:
    """Internal entry, channel inherited only by this child (not completion jobs)."""
    global _child_channel, _child_deadline
    _child_channel = socket.socket(fileno=int(sys.argv[3]))
    _child_channel.set_inheritable(False)
    _child_deadline = float(sys.argv[4])
    try:
        from hermes_cli import main
        main.cmd_update(argparse.Namespace(**json.loads(sys.argv[2])))
    finally:
        _child_channel.close()
