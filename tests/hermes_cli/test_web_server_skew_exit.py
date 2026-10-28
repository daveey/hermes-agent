"""A Desktop-over-SSH ``serve --isolated`` backend retires itself once the install under it moved to
new code: nothing on its host may restart it (only the remote client holds its token), so without
this it runs the pre-update code against the updated tree until that client happens to reconnect.
It exits only between turns (the retirement fence proves idle and closes admission first), never
while an update is still in flight, and its client respawns it from the new code."""

from __future__ import annotations

import subprocess

from hermes_cli.web_server_skew_exit import should_retire_for_skew, start_code_skew_watchdog

_SKEW = ("aaaaaaaaaa", "bbbbbbbbbb")


class _Server:
    should_exit = False


class _Fence:
    """Retirement fence stand-in: ``idle`` answers prepare; commit records the exit permit."""

    def __init__(self, idle: bool):
        self.idle, self.committed = idle, False

    def prepare(self):
        return {"ok": True, "idle": True, "token": "t"} if self.idle else {"ok": False, "idle": False}

    def commit(self, token):
        self.committed = token == "t"
        return {"ok": self.committed}


def test_retires_only_on_a_proven_code_change_outside_an_update():
    assert should_retire_for_skew(skew=_SKEW, update_in_progress=False) is True
    # No drift, or an unreadable revision (detect_code_skew returns None for both): stay up.
    assert should_retire_for_skew(skew=None, update_in_progress=False) is False
    # The tree is mid-swap: respawning now would import a half-updated install.
    assert should_retire_for_skew(skew=_SKEW, update_in_progress=True) is False


def _run(fence, skews, *, update=False):
    server = _Server()
    seq = iter(skews)
    thread = start_code_skew_watchdog(
        server, skew_fn=lambda: next(seq, skews[-1]),
        update_probe=lambda: update, fence=fence, poll_s=0.01, max_polls=len(skews) + 2)
    thread.join(timeout=5)
    return server


def test_skew_retires_through_the_fence_after_two_consecutive_observations():
    fence = _Fence(idle=True)
    assert _run(fence, [_SKEW, _SKEW]).should_exit is True
    assert fence.committed is True  # admission closed before exit: no turn can start mid-teardown


def test_a_single_blip_does_not_retire():
    fence = _Fence(idle=True)
    assert _run(fence, [_SKEW, None, _SKEW, None]).should_exit is False
    assert fence.committed is False


def test_a_busy_backend_waits_instead_of_cutting_off_its_client():
    fence = _Fence(idle=False)
    assert _run(fence, [_SKEW, _SKEW, _SKEW]).should_exit is False


def test_never_retires_while_an_update_is_in_flight():
    fence = _Fence(idle=True)
    assert _run(fence, [_SKEW, _SKEW, _SKEW], update=True).should_exit is False


def test_default_probe_retires_once_the_checkout_moves_past_the_boot_revision(tmp_path, monkeypatch):
    """The real wiring: the watchdog reads the boot revision the serve lifespan records
    (``gateway.code_skew``) against a checkout whose HEAD then moves."""
    from gateway import code_skew

    repo = tmp_path / "repo"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    monkeypatch.setattr(code_skew, "_PROJECT_ROOT", repo)
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
    code_skew.record_boot_fingerprint()

    fence = _Fence(idle=True)
    unchanged = _Server()
    start_code_skew_watchdog(unchanged, update_probe=lambda: False, fence=fence,
                             poll_s=0.01, max_polls=4).join(timeout=5)
    assert unchanged.should_exit is False

    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "two"], check=True)
    moved = _Server()
    start_code_skew_watchdog(moved, update_probe=lambda: False, fence=fence,
                             poll_s=0.01, max_polls=4).join(timeout=5)
    assert moved.should_exit is True


def _run_runtime(fence, intact, *, update=False, **kw):
    server = _Server()
    seq = iter(intact)
    kw.setdefault("runtime_reattach", lambda: False)
    if "ledger" not in kw:
        from hermes_cli.web_server_skew_exit import RuntimeRetireLedger
        import tempfile
        from pathlib import Path

        kw["ledger"] = RuntimeRetireLedger(Path(tempfile.mkdtemp()) / "ledger.json")
    thread = start_code_skew_watchdog(
        server, skew_fn=lambda: None, update_probe=lambda: update,
        runtime_probe=lambda: next(seq, intact[-1]), fence=fence, poll_s=0.01, max_polls=len(intact) + 2,
        runtime_key=lambda: "8d94c4a7dd14f7b1", **kw)
    thread.join(timeout=5)
    return server


def test_a_replaced_runtime_retires_even_when_the_revision_did_not_move():
    # A runtime rebuild swapped site-packages but left the checkout revision alone: clients see
    # runtimeIntact false and refuse reuse, so the backend must retire instead of sitting stale.
    fence = _Fence(idle=True)
    assert _run_runtime(fence, [False, False]).should_exit is True
    assert fence.committed is True


def test_an_intact_runtime_or_a_single_blip_stays_up():
    assert _run_runtime(_Fence(idle=True), [True, True]).should_exit is False
    assert _run_runtime(_Fence(idle=True), [False, True, False, True]).should_exit is False


def test_a_replaced_runtime_waits_for_idle_and_for_the_update_to_finish():
    assert _run_runtime(_Fence(idle=False), [False, False]).should_exit is False
    assert _run_runtime(_Fence(idle=True), [False, False], update=True).should_exit is False


class _TurnFence(_Fence):
    """Busy for the first ``busy`` prepares (a Desktop turn is running), idle after."""

    def __init__(self, busy: int):
        super().__init__(idle=True)
        self.busy, self.prepares = busy, 0

    def prepare(self):
        self.prepares += 1
        if self.prepares <= self.busy:
            return {"ok": False, "idle": False}
        return super().prepare()


def test_a_real_runtime_swap_retires_once_between_turns_and_is_recorded(tmp_path):
    """[daveey] The marker is gone and the bytes changed (no reattach): retire, but only after the
    running turn ends, and record it so the respawned backend knows a retirement just happened."""
    from hermes_cli.web_server_skew_exit import RuntimeRetireLedger

    ledger = RuntimeRetireLedger(tmp_path / "ledger.json")
    fence = _TurnFence(busy=3)
    server = _run_runtime(fence, [False] * 8, ledger=ledger, clock=lambda: 1000.0)
    assert server.should_exit is True
    assert fence.prepares == 4 and fence.committed is True  # held through 3 busy polls
    assert ledger.last("8d94c4a7dd14f7b1") == 1000.0


def test_an_identical_rerealization_reattaches_instead_of_retiring(tmp_path):
    from hermes_cli.web_server_skew_exit import RuntimeRetireLedger

    ledger = RuntimeRetireLedger(tmp_path / "ledger.json")
    fence = _Fence(idle=True)
    server = _run_runtime(fence, [False, False, False], ledger=ledger, runtime_reattach=lambda: True)
    assert server.should_exit is False and fence.committed is False
    assert ledger.last("8d94c4a7dd14f7b1") is None


def test_the_respawned_backend_does_not_retire_again_inside_the_backoff_window(tmp_path):
    """[daveey] Restart-loop guard: the always-on dashboard respawns on the same nonce, so a cause
    that keeps deleting the marker must not retire it every minute. Inside the window it stays up;
    after the window a still-missing marker retires once more."""
    from hermes_cli.web_server_skew_exit import RUNTIME_RETIRE_BACKOFF_S, RuntimeRetireLedger

    ledger = RuntimeRetireLedger(tmp_path / "ledger.json")
    ledger.record("8d94c4a7dd14f7b1", 1000.0)

    inside = _Fence(idle=True)
    held = _run_runtime(inside, [False] * 5, ledger=ledger, clock=lambda: 1000.0 + 70)
    assert held.should_exit is False and inside.committed is False

    # Another nonce (a different Desktop's backend) is not held by this one's retirement.
    other = _Fence(idle=True)
    server = _Server()
    start_code_skew_watchdog(server, skew_fn=lambda: None, update_probe=lambda: False,
                             runtime_probe=lambda: False, runtime_reattach=lambda: False, ledger=ledger,
                             runtime_key=lambda: "0123456789abcdef", clock=lambda: 1070.0, fence=other,
                             poll_s=0.01, max_polls=4).join(timeout=5)
    assert server.should_exit is True

    after = _Fence(idle=True)
    late = _run_runtime(after, [False] * 3, ledger=ledger, clock=lambda: 1000.0 + RUNTIME_RETIRE_BACKOFF_S + 1)
    assert late.should_exit is True and after.committed is True


def test_runtime_retire_ledger_reads_a_bom_prefixed_file(tmp_path):
    """A PowerShell-written ledger still holds the backoff. utf-8 (not utf-8-sig) makes json.loads raise and the guard looks empty."""
    from hermes_cli.web_server_skew_exit import RuntimeRetireLedger

    path = tmp_path / "ledger.json"
    path.write_bytes(b"\xef\xbb\xbf" + b'{"8d94c4a7dd14f7b1": 1000.0}\n')
    assert RuntimeRetireLedger(path).last("8d94c4a7dd14f7b1") == 1000.0


def test_the_backoff_never_holds_a_code_revision_retirement(tmp_path):
    from hermes_cli.web_server_skew_exit import RuntimeRetireLedger

    ledger = RuntimeRetireLedger(tmp_path / "ledger.json")
    ledger.record("8d94c4a7dd14f7b1", 1000.0)
    server = _Server()
    start_code_skew_watchdog(server, skew_fn=lambda: _SKEW, update_probe=lambda: False,
                             runtime_probe=lambda: False, runtime_reattach=lambda: False, ledger=ledger,
                             runtime_key=lambda: "8d94c4a7dd14f7b1", clock=lambda: 1010.0,
                             fence=_Fence(idle=True), poll_s=0.01, max_polls=4).join(timeout=5)
    assert server.should_exit is True
