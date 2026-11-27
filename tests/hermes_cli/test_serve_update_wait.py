"""[daveey] ``hermes serve`` must not load the agent stack while a core update is about to swap it.

David-MBP 2026-10-28: a Desktop-spawned SSH backend booted at 00:51:23, between hermes-sync's
admission hold (00:51:19) and the checkout move (00:51:44), and served pre-update code for 41 min.
"""

from __future__ import annotations

from contextlib import nullcontext
import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from hermes_cli import serve_update_wait as suw


# ── cmd_dashboard harness: everything but the update gate is stubbed ─────────────────────────


@pytest.fixture
def serve_harness(monkeypatch):
    import hermes_cli.main as main_mod

    calls: list[str] = []
    started = threading.Event()

    for name in ("_dashboard_lifecycle_flags", "_dashboard_sanitize_desktop_env", "_attach_to_host_backend",
                 "_route_named_profile_dashboard", "_maybe_setup_dashboard_auth_interactively"):
        monkeypatch.setattr(main_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_dashboard_validate_serve_args", lambda *a, **k: "nonce")
    monkeypatch.setattr(main_mod, "_dashboard_prepare_runtime", lambda *a, **k: False)
    monkeypatch.setattr(main_mod, "_ssh_bootstrap_server_kwargs", lambda *a, **k: {})

    def _read_token(path):
        calls.append("token")
        return "tok"

    monkeypatch.setattr(main_mod, "_read_ssh_session_token_file", _read_token)

    import hermes_cli.resource_limits as rl
    monkeypatch.setattr(rl, "apply_nofile_soft_limit", lambda: None)
    import hermes_cli.observability.shared_metrics_process as smp
    monkeypatch.setattr(smp, "begin_process", lambda *a, **k: None)

    def _start_server(**kwargs):
        # Unrelated startup work may yield; only update-gate sleeps are the oracle.
        time.sleep(0)
        calls.append("start_server")
        started.set()

    fake_web = types.ModuleType("hermes_cli.web_server")
    fake_web.start_server = _start_server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", fake_web)
    monkeypatch.setenv("HERMES_SERVE_UPDATE_POLL_S", "0.02")
    # A fresh serve process: no identity cached by an earlier test. And never exec over pytest:
    # the re-exec path is exercised for real in a child process below.
    import hermes_cli.version_info as vi
    monkeypatch.setattr(vi, "_cached_version_info", None)
    monkeypatch.setattr(vi, "prewarm_version_info", lambda: None)
    from hermes_cli import sessions_early_boot, serve_bootstrap_listener
    monkeypatch.setattr(sessions_early_boot, "early_sessions", lambda **kw: nullcontext(None))
    monkeypatch.setattr(serve_bootstrap_listener, "BootstrapListener", lambda *a: nullcontext(None))

    def _no_exec(*_a):
        raise AssertionError("cmd_dashboard tried to re-exec the test process")

    monkeypatch.setattr(os, "execv", _no_exec)
    monkeypatch.setattr(suw, "checkout_fingerprint", lambda: "git:refs/heads/main:" + "a" * 40)

    args = types.SimpleNamespace(
        ssh_session_token_file="/nonexistent/token", headless_backend=True, isolated=True,
        host="127.0.0.1", port=0, no_open=True, insecure=False, open_profile="",
    )

    def run_in_thread():
        t = threading.Thread(target=main_mod.cmd_dashboard, args=(args,), daemon=True)
        t.start()
        return t

    return types.SimpleNamespace(calls=calls, started=started, run=run_in_thread,
                                 call=lambda: main_mod.cmd_dashboard(args))


def test_serve_started_while_update_live_does_not_serve_until_it_clears(serve_harness, monkeypatch):
    held = threading.Event()
    held.set()
    monkeypatch.setattr(suw, "update_live", held.is_set)
    monkeypatch.setattr(suw, "admission_hold_present", lambda: False)

    thread = serve_harness.run()
    assert not serve_harness.started.wait(0.6), "serve started while the update marker was live"
    assert serve_harness.calls == [], "the one-shot SSH token was consumed before the update cleared"

    held.clear()
    assert serve_harness.started.wait(3.0), "serve never started after the update cleared"
    thread.join(3.0)
    assert serve_harness.calls == ["token", "start_server"]


@pytest.mark.parametrize("held_once", [False, True])
def test_serve_with_no_update_starts_immediately(serve_harness, monkeypatch, held_once):
    probes = iter([held_once, False])
    monkeypatch.setattr(suw, "update_live", lambda: next(probes, False))
    monkeypatch.setattr(suw, "admission_hold_present", lambda: False)
    slept: list[float] = []
    wait = suw.wait_for_update_clear
    monkeypatch.setattr(suw, "wait_for_update_clear", lambda: wait(sleep=slept.append))

    t0 = time.monotonic()
    serve_harness.call()
    assert serve_harness.calls == ["token", "start_server"]
    assert slept == ([0.02] if held_once else [])
    assert time.monotonic() - t0 < 1.0


def test_serve_waits_on_a_real_admission_hold_file(serve_harness, monkeypatch, tmp_path):
    # The incident's live signal: hermes-sync's hold, set before `hermes update` wrote any marker.
    monkeypatch.setattr(suw, "update_live", lambda: False)
    hold = tmp_path / "core-update" / "holds" / "hermes-sync"
    hold.parent.mkdir(parents=True)
    hold.write_text("x")
    real_hold = suw.admission_hold_present
    assert real_hold(tmp_path) is True
    monkeypatch.setattr(suw, "admission_hold_present", lambda: real_hold(tmp_path))

    thread = serve_harness.run()
    assert not serve_harness.started.wait(0.6), "serve started while the admission hold was set"
    hold.unlink()
    assert serve_harness.started.wait(3.0)
    thread.join(3.0)


def test_serve_waits_on_a_real_marker_held_by_a_live_updater(serve_harness, monkeypatch, tmp_path):
    """End to end on the real marker protocol: another process holds UpdateLock."""
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "install").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    monkeypatch.setattr(suw, "admission_hold_present", lambda: False)
    repo = Path(__file__).resolve().parents[2]
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(repo)!r})
            from hermes_cli.update_lock import UpdateLock
            lock = UpdateLock(install_root={str(tmp_path / 'install')!r})
            print('acquired' if lock.acquire() else 'refused', flush=True)
            sys.stdin.readline()
        """)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    try:
        assert holder.stdout.readline().strip() == "acquired"
        assert (home / ".hermes-update-in-progress").exists()
        assert suw.update_live() is True
        thread = serve_harness.run()
        assert not serve_harness.started.wait(0.8), "serve started while a live updater held the marker"
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(10)
        assert serve_harness.started.wait(5.0), "serve never started after the updater released"
        thread.join(3.0)
    finally:
        if holder.poll() is None:
            holder.kill()


# ── the wait policy itself ────────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_wait_is_bounded_and_logged_then_proceeds():
    clock, lines = _Clock(), []
    out = suw.wait_for_update_clear(update_probe=lambda: True, hold_probe=lambda: False,
                                    update_timeout_s=10, sleep=clock.sleep, now=clock.now, log=lines.append)
    assert out == "update-timeout"
    assert 10 <= clock.t <= 11
    assert any("waiting" in line for line in lines) and any("starting anyway" in line for line in lines)


def test_hold_alone_has_a_shorter_bound():
    clock, lines = _Clock(), []
    out = suw.wait_for_update_clear(update_probe=lambda: False, hold_probe=lambda: True,
                                    update_timeout_s=600, hold_timeout_s=5,
                                    sleep=clock.sleep, now=clock.now, log=lines.append)
    assert out == "hold-timeout" and 5 <= clock.t <= 6


def test_hold_then_update_uses_the_update_bound_and_ignores_the_trailing_hold():
    clock, lines = _Clock(), []

    def update():  # hold at t<3, update 3..100, trailing hold after
        return 3 <= clock.t < 100

    out = suw.wait_for_update_clear(update_probe=update, hold_probe=lambda: True,
                                    update_timeout_s=600, hold_timeout_s=5,
                                    sleep=clock.sleep, now=clock.now, log=lines.append)
    assert out == "cleared"
    assert 100 <= clock.t <= 101


def test_zero_bound_disables_the_gate():
    assert suw.wait_for_update_clear(update_probe=lambda: True, hold_probe=lambda: True,
                                     update_timeout_s=0, log=lambda s: None) == "clear"


def test_update_spawned_by_our_own_updater_is_not_waited_on(monkeypatch):
    from hermes_cli import update_lock as ul

    monkeypatch.setattr(ul, "read_live_update", lambda **k: ul.UpdateHolder(pid=os.getppid(), age_seconds=1.0))
    assert suw.update_live() is False
    monkeypatch.setattr(ul, "read_live_update", lambda **k: ul.UpdateHolder(pid=1, age_seconds=1.0))
    monkeypatch.setattr(ul, "_is_ancestor_pid", lambda pid: False)
    assert suw.update_live() is True
