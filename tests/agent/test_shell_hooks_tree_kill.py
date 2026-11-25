"""Process-tree cleanup for timed-out shell hooks (port of openai/codex#37527).

Timing out a shell hook must not leave descendant processes running after the
hook itself is stopped. ``_spawn`` places the hook in its own process group on
POSIX (``process_group=0``) and, on timeout, ``kill_process_tree`` signals the
whole group — gated on the child actually leading its own group so a
shared-group spawn can never take down unrelated processes. Hooks that
complete within their timeout keep their descendants (intentionally detached
helpers survive a successful run).

These tests use REAL subprocesses (no mocks): group membership and survival
semantics cannot be mocked.
"""

import os
import signal
import sys
import textwrap
import threading
import time

import pytest

from agent import shell_hooks
from agent.shell_hooks import ShellHookSpec, _spawn
from hermes_cli import worker_drain_fence

pytestmark = pytest.mark.platforms("posix")  # POSIX process-group semantics


def _spec(command: str, timeout: int = 2) -> ShellHookSpec:
    return ShellHookSpec(
        event="post_tool_call",
        command=command,
        matcher=None,
        timeout=timeout,
        fail_closed=False,
    )


def _write_forking_script(tmp_path, stall_after: bool):
    """A hook that forks a long-lived descendant, then stalls or exits."""
    marker = tmp_path / "child.pid"
    script = tmp_path / "hook.sh"
    tail = "sleep 300" if stall_after else "exit 0"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            sleep 300 &
            echo $! > {marker}
            {tail}
            """
        )
    )
    script.chmod(0o755)
    return script, marker


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_marker(marker, timeout=5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return int(marker.read_text().strip())
        time.sleep(0.05)
    raise AssertionError("hook script never wrote its descendant pid")


def test_timeout_kills_descendants(tmp_path):
    """A hook timeout must take the forked descendant down with the hook."""
    script, marker = _write_forking_script(tmp_path, stall_after=True)

    t0 = time.monotonic()
    r = _spawn(_spec(str(script), timeout=1), "{}")
    elapsed = time.monotonic() - t0

    assert r["timed_out"] is True
    assert elapsed < 30, "timeout cleanup must not hang on held pipes"

    child_pid = _read_marker(marker)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _pid_alive(child_pid):
        time.sleep(0.05)
    alive = _pid_alive(child_pid)
    if alive:  # cleanup so a failure doesn't leak a 300s sleeper
        os.kill(child_pid, 9)
    assert not alive, f"descendant {child_pid} survived hook timeout"


@pytest.mark.live_system_guard_bypass  # cleanup-kills a helper reparented to init
def test_successful_hook_preserves_detached_helpers(tmp_path):
    """A hook that completes in time keeps its intentionally detached helpers.

    Mirrors codex#37527's "preserve descendants on success" semantics: tree
    cleanup fires only on the timeout/error path.
    """
    script, marker = _write_forking_script(tmp_path, stall_after=False)
    # Detach the helper's stdio so the pipes reach EOF despite the survivor.
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            sleep 300 > /dev/null 2>&1 < /dev/null &
            echo $! > {marker}
            exit 0
            """
        )
    )

    r = _spawn(_spec(str(script), timeout=10), "{}")
    assert r["timed_out"] is False
    assert r["returncode"] == 0

    child_pid = _read_marker(marker)
    time.sleep(0.3)
    alive = _pid_alive(child_pid)
    if alive:
        os.kill(child_pid, 9)
    assert alive, "successful hook's detached helper must survive"


def test_hook_child_leads_own_process_group(tmp_path):
    """The hook child must lead its own group (killpg ownership precondition)."""
    script = tmp_path / "pgid.sh"
    script.write_text("#!/usr/bin/env bash\necho \"$$ $(ps -o pgid= -p $$ | tr -d ' ')\"\n")
    script.chmod(0o755)

    r = _spawn(_spec(str(script), timeout=10), "{}")
    assert r["returncode"] == 0
    pid, pgid = r["stdout"].split()
    assert pid == pgid, f"hook child pid={pid} does not lead its group pgid={pgid}"
    assert int(pgid) != os.getpgid(0), "hook child must not share our group"


def test_fast_path_contract_unchanged(tmp_path):
    """stdin JSON delivery, stdout/stderr capture, and exit codes still work."""
    script = tmp_path / "echoer.sh"
    script.write_text(
        "#!/usr/bin/env bash\ncat\necho errline >&2\nexit 3\n"
    )
    script.chmod(0o755)

    r = _spawn(_spec(str(script), timeout=10), '{"tool_name": "terminal"}')
    assert r["returncode"] == 3
    assert r["stdout"] == '{"tool_name": "terminal"}'
    assert "errline" in r["stderr"]
    assert r["error"] is None
    assert r["timed_out"] is False


def test_interrupt_kills_hook_and_propagates(tmp_path):
    """Ctrl+C during a hook must reap the hook, then let KeyboardInterrupt through.

    The hook leads its own process group, so the terminal's SIGINT never
    reaches it: only ``_spawn``'s own cleanup can stop it.
    """
    marker = tmp_path / "hook.pid"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/usr/bin/env bash\necho $$ > "{marker}"\nsleep 300\n', encoding="utf-8")
    script.chmod(0o755)

    def interrupt_once_running():
        """Interrupt only once the hook is up, so the signal lands inside _spawn."""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text().strip():
                os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.05)

    interrupter = threading.Thread(target=interrupt_once_running, daemon=True)
    interrupter.start()
    with pytest.raises(KeyboardInterrupt):
        _spawn(_spec(str(script), timeout=300), "{}")
    interrupter.join(timeout=5)

    hook_pid = _read_marker(marker)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _pid_alive(hook_pid):
        time.sleep(0.05)
    alive = _pid_alive(hook_pid)
    if alive:  # cleanup so a failure doesn't leak a 300s sleeper
        os.kill(hook_pid, 9)
    assert not alive, f"hook {hook_pid} survived the interrupt"


@pytest.mark.parametrize("when", ["before_create", "before_handoff"])
def test_interrupt_at_spawn_boundary_reaps_real_hook(monkeypatch, tmp_path, when):
    """OS child readiness can precede the parent's popen_worker return."""
    original = worker_drain_fence.popen_worker
    children = []
    previous = signal.getsignal(signal.SIGINT)
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\nsleep 300\n")
    script.chmod(0o755)

    def interrupted_handoff(popen, *args, **kwargs):
        if when == "before_create":
            signal.raise_signal(signal.SIGINT)
        proc = original(popen, *args, **kwargs)
        children.append(proc)
        if when == "before_handoff":
            signal.raise_signal(signal.SIGINT)
        return proc

    monkeypatch.setattr(worker_drain_fence, "popen_worker", interrupted_handoff)
    try:
        with pytest.raises(KeyboardInterrupt):
            _spawn(_spec(str(script), timeout=300), "{}")
        assert signal.getsignal(signal.SIGINT) is previous
        assert len(children) == 1
        assert children[0].poll() is not None, "hook survived interrupted spawn handoff"
    finally:
        for proc in children:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)


def test_interrupt_before_spawn_has_no_cleanup_target(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    def forbidden(*args):
        pytest.fail("no child exists to clean up")

    previous = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(worker_drain_fence, "popen_worker", interrupted)
    monkeypatch.setattr(shell_hooks, "kill_process_tree", forbidden)
    with pytest.raises(KeyboardInterrupt):
        _spawn(_spec("unused"), "{}")
    assert signal.getsignal(signal.SIGINT) is previous


def test_spawn_deferral_preserves_custom_handler(monkeypatch, tmp_path):
    original = worker_drain_fence.popen_worker
    children = []
    calls = []
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\nsleep 300\n")
    script.chmod(0o755)

    def custom(signum, frame):
        calls.append(signum)
        raise RuntimeError("custom interrupt")

    def interrupted_handoff(popen, *args, **kwargs):
        proc = original(popen, *args, **kwargs)
        children.append(proc)
        signal.raise_signal(signal.SIGINT)
        assert not calls, "handler must run only after the handle is owned"
        return proc

    previous = signal.signal(signal.SIGINT, custom)
    monkeypatch.setattr(worker_drain_fence, "popen_worker", interrupted_handoff)
    try:
        result = _spawn(_spec(str(script)), "{}")
        assert result["error"] == "custom interrupt"
        assert calls == [signal.SIGINT]
        assert signal.getsignal(signal.SIGINT) is custom
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        signal.signal(signal.SIGINT, previous)
        for proc in children:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)


@pytest.mark.parametrize("handler", [signal.SIG_IGN, signal.SIG_DFL])
def test_spawn_deferral_leaves_noncallable_handler_untouched(monkeypatch, handler):
    previous = signal.signal(signal.SIGINT, handler)

    def forbidden(*args):
        pytest.fail("noncallable handler must not be replaced")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(shell_hooks.signal, "signal", forbidden)
            with shell_hooks._defer_hook_spawn_sigint():
                assert signal.getsignal(signal.SIGINT) == handler
    finally:
        signal.signal(signal.SIGINT, previous)


def test_spawn_deferral_in_worker_does_not_change_global_handler(monkeypatch):
    previous = signal.getsignal(signal.SIGINT)
    entered = []
    errors = []

    def forbidden(*args):
        pytest.fail("worker thread must not change process signal disposition")

    def worker():
        try:
            with shell_hooks._defer_hook_spawn_sigint():
                entered.append(True)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(shell_hooks.signal, "signal", forbidden)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert entered == [True]
    assert signal.getsignal(signal.SIGINT) is previous


def test_spawn_deferral_does_not_ignore_sigint_in_child(tmp_path):
    script = tmp_path / "self-interrupt.sh"
    script.write_text("#!/bin/sh\nkill -INT $$\necho incorrectly-ignored\n")
    script.chmod(0o755)
    result = _spawn(_spec(str(script)), "{}")
    assert result["returncode"] == -signal.SIGINT
    assert result["stdout"] == ""


def test_missing_command_still_fails_open():
    r = _spawn(_spec("/nonexistent/hook-command-xyz", timeout=2), "{}")
    assert r["error"] == "command not found"
    assert r["returncode"] is None
