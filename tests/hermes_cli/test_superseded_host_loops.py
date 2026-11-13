"""A backend being replaced gives its host loops up at once (t_01a1d65520e78e7d). [daveey]

Hub 2026-11-13: the always-on backend :9148 (booted on 17f28630e4, before a fix) took the ``kanban-unloaded``
lease while the gateway restarted on the fix (19:10:13), was superseded at 19:11:14, and still ran a sweep wake
during its drain at 19:11:19. That wake failed on the pre-fix code (SubscriptionQuotaUnavailable) and booked its
replay at the quota wall's reset, 4 days out. A superseded backend, or one whose boot commit is behind the
checkout, now releases every host-loop lease, refuses them while it drains, starts no automation wake, and a
build failure in it never defers a delivery to a wall reset.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

import pytest

from hermes_cli import host_loop_lease as hll
from hermes_cli import web_server_skew_exit as skew

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

pytestmark = pytest.mark.skipif(fcntl is None, reason="flock leases need fcntl")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(hll.KILL_SWITCH_ENV, raising=False)
    skew._DRAINING.clear()
    with hll._RETIRE_MU:
        hll._RETIRED.clear()
    yield
    skew._DRAINING.clear()
    with hll._RETIRE_MU:
        hll._RETIRED.clear()


def _lock_is_free(lease: hll.HostLoopLease) -> bool:
    """Whether another open file description can take the lease's flock right now."""
    fd = os.open(lease.path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


class _Server:
    should_exit = False


class _BusyFence:
    """A live turn refuses the retirement fence: the backend keeps draining (the window the bug lived in)."""

    def prepare(self):
        return {"ok": False}

    def commit(self, token):  # pragma: no cover - never reached
        return {"ok": False}


def _held_lease(tmp_path) -> hll.HostLoopLease:
    lease = hll.HostLoopLease("kanban-unloaded", "serve", interval=30, home=tmp_path)
    assert lease.held() is True and not _lock_is_free(lease)
    return lease


# ── the lease ──────────────────────────────────────────────────────────────────────────────────────────


def test_retiring_releases_every_held_lease_and_refuses_it_until_resumed(tmp_path, monkeypatch):
    lease = _held_lease(tmp_path)
    assert hll.retire_host_loops("test", "superseded") is True
    assert hll.host_loops_retired() == "superseded"
    assert _lock_is_free(lease)  # the gateway / successor can take it at once
    assert lease.held() is False and lease.exclusive() is False
    monkeypatch.setenv(hll.KILL_SWITCH_ENV, "0")
    assert lease.held() is False  # a retiring process runs no host pass, kill switch or not
    monkeypatch.delenv(hll.KILL_SWITCH_ENV)
    assert hll.resume_host_loops("test") is True
    assert lease.held() is True


def test_every_reason_must_clear_before_the_process_competes_again(tmp_path):
    lease = _held_lease(tmp_path)
    hll.retire_host_loops("a", "superseded")
    assert hll.retire_host_loops("b", "code skew") is False  # already retired
    assert hll.resume_host_loops("a") is False and lease.held() is False
    assert hll.resume_host_loops("b") is True and lease.held() is True


@contextmanager
def _retiring_before_release(lease, monkeypatch):
    """Pause the real retirement sweep after publication, before it releases this lease.

    The registry does not make the race guards redundant: _RETIRED is published before
    release() can acquire each lease's mutex. Events hold precisely that scheduling gap.
    """
    reached, finish = threading.Event(), threading.Event()
    release = lease.release

    def delayed_release():
        reached.set()
        finish.wait()
        release()

    monkeypatch.setattr(lease, "release", delayed_release)
    thread = threading.Thread(target=hll.retire_host_loops, args=("race", "retiring"))
    thread.start()
    try:
        assert reached.wait(10), "retirement did not reach the lease release"
        assert hll.host_loops_retired() == "retiring"
        yield
    finally:
        finish.set()
        thread.join(10)
        assert not thread.is_alive()


def test_exclusive_false_while_retired_even_if_held(tmp_path, monkeypatch):
    lease = _held_lease(tmp_path)
    try:
        with _retiring_before_release(lease, monkeypatch):
            assert lease._held and not _lock_is_free(lease)
            assert lease.exclusive() is False
    finally:
        hll.HostLoopLease.release(lease)


def test_retire_during_handoff_wait_returns_the_lock(tmp_path, monkeypatch):
    lease = hll.HostLoopLease("handoff", "gateway", interval=30, home=tmp_path)
    fd = lease._open()
    assert fd is not None
    blocker = os.open(lease.path, os.O_RDWR)
    fcntl.flock(blocker, fcntl.LOCK_EX)
    waiting = threading.Event()
    flock = fcntl.flock

    def observed_flock(lock_fd, operation):
        if lock_fd == fd and operation == fcntl.LOCK_EX:
            waiting.set()
        return flock(lock_fd, operation)

    monkeypatch.setattr(fcntl, "flock", observed_flock)
    lease._waiting = True
    assert lease._publish_want()
    thread = threading.Thread(target=lease._wait_for_lock, args=(fd,))
    thread.start()
    try:
        assert waiting.wait(10)
        with _retiring_before_release(lease, monkeypatch):
            flock(blocker, fcntl.LOCK_UN)
            thread.join(10)
            assert not thread.is_alive()
            # Check before the sweep releases the object; otherwise release masks A4.
            assert lease._fd == fd and not lease._waiting
            assert not lease._held and _lock_is_free(lease)
            assert not lease.want_path.exists()
    finally:
        flock(blocker, fcntl.LOCK_UN)
        os.close(blocker)
        thread.join(10)
        hll.HostLoopLease.release(lease)
        assert not thread.is_alive()


def test_retire_between_lock_win_and_recheck_releases(tmp_path, monkeypatch):
    lease = hll.HostLoopLease("lock-win", "serve", interval=30, home=tmp_path)
    retirement = _retiring_before_release(lease, monkeypatch)
    entered = []

    def retire_after_lock_win():
        assert not _lock_is_free(lease)
        retirement.__enter__()
        entered.append(True)
        # Implicit None: no higher-priority want can mask the retirement arm.

    monkeypatch.setattr(lease, "_live_want", retire_after_lock_win)
    try:
        assert lease.held() is False
        assert entered and not lease._held
        assert _lock_is_free(lease)
    finally:
        if entered:
            retirement.__exit__(None, None, None)
        hll.HostLoopLease.release(lease)


def test_one_skew_read_below_confirmations_does_not_retire(tmp_path):
    lease = _held_lease(tmp_path)
    stale = skew.StaleCodeLoops(lambda: ("boot", "disk"))
    try:
        assert skew._CONFIRMATIONS == 2
        stale.observe()
        # Observe the transient state, not just recovery after a later non-skew read.
        assert not stale.retired and hll.host_loops_retired() is None
        assert lease.exclusive() and not _lock_is_free(lease)
        stale.observe()
        assert stale.retired and hll.host_loops_retired()
        assert not lease.exclusive() and _lock_is_free(lease)
    finally:
        lease.release()


# ── the watchdogs ─────────────────────────────────────────────────────────────────────────────────────


def test_a_superseded_backend_releases_the_lease_on_the_first_observation_while_it_drains(tmp_path):
    lease = _held_lease(tmp_path)
    server = _Server()
    states = []

    def superseded(port):
        states.append((hll.host_loops_retired(), _lock_is_free(lease)))
        return True

    skew.start_superseded_watchdog(server, port=9148, superseded=superseded, fence=_BusyFence(), poll_s=0.001,
                                   max_polls=3, http_connections=lambda: 2, skew_fn=lambda: None,
                                   stale_check_s=0).join(10)
    assert states[0] == (None, False)  # held until the switch is seen
    assert states[1][0] and states[1][1] is True  # from the next poll on: retired, lock free, still draining
    assert server.should_exit is False and skew.draining() is True
    assert lease.held() is False


def test_an_aborted_roll_gives_the_host_loops_back(tmp_path):
    lease = _held_lease(tmp_path)
    flips = iter([True, True, False])
    skew.start_superseded_watchdog(_Server(), port=9148, superseded=lambda p: next(flips, False),
                                   fence=_BusyFence(), poll_s=0.001, max_polls=3, http_connections=lambda: 2,
                                   skew_fn=lambda: None, stale_check_s=0).join(10)
    assert hll.host_loops_retired() is None and lease.held() is True


def test_an_always_on_backend_behind_the_checkout_gives_the_lease_up_before_it_is_superseded(tmp_path):
    """:9148 took the lease at 19:10:13, ten seconds after the checkout moved past its boot commit."""
    lease = _held_lease(tmp_path)
    reads = iter([None, ("17f28630e4", "0068f2ca86"), ("17f28630e4", "0068f2ca86")])
    skew.start_superseded_watchdog(_Server(), port=9148, superseded=lambda p: False, fence=_BusyFence(),
                                   poll_s=0.001, max_polls=3, skew_fn=lambda: next(reads, None),
                                   stale_check_s=0).join(10)
    assert "17f28630e4" in (hll.host_loops_retired() or "")
    assert _lock_is_free(lease) and lease.held() is False


def test_a_single_skew_blip_keeps_the_lease():
    reads = iter([("a", "b"), None, ("a", "b"), None])
    skew.start_superseded_watchdog(_Server(), port=9148, superseded=lambda p: False, fence=_BusyFence(),
                                   poll_s=0.001, max_polls=4, skew_fn=lambda: next(reads, None),
                                   stale_check_s=0).join(10)
    assert hll.host_loops_retired() is None


def test_a_code_skewed_backend_waiting_for_its_fence_runs_no_host_pass(tmp_path):
    lease = _held_lease(tmp_path)
    reads = iter([("aaa", "bbb")] * 3 + [None])
    skew.start_code_skew_watchdog(_Server(), skew_fn=lambda: next(reads, None), update_probe=lambda: False,
                                  runtime_probe=lambda: True, fence=_BusyFence(), poll_s=0.001,
                                  max_polls=3).join(10)
    assert "aaa" in (hll.host_loops_retired() or "") and lease.held() is False
    stale = skew.StaleCodeLoops(lambda: None)
    stale.retired = True
    stale.observe()  # the checkout reads as the boot commit again
    assert hll.host_loops_retired() is None and lease.held() is True


# ── no new wake on stale code ─────────────────────────────────────────────────────────────────────────


def test_a_retired_backend_starts_no_sweep_wake_and_runs_no_residual_pass(tmp_path, monkeypatch):
    from tui_gateway import kanban_unloaded as ku

    ran = []
    monkeypatch.setattr(ku, "host_pass", lambda *a, **k: ran.append("host"))
    monkeypatch.setattr(ku, "residual_pass", lambda: ran.append("residual"))
    lease = _held_lease(tmp_path)  # it held the lease when the switch came, as :9148 did
    hll.retire_host_loops("test", "always-on backend :9148 superseded")
    assert ku.run_pass(lease) == "retired" and ran == []


def test_a_retired_backend_refuses_the_wake_before_resuming_or_building(monkeypatch):
    from tui_gateway import server

    touched = []
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key: touched.append(key), raising=False)
    monkeypatch.setitem(server._methods, "session.resume", lambda rid, p: touched.append(rid) or {})
    hll.retire_host_loops("test", "always-on backend :9148 superseded")
    verdict = server._wake_session_turn("20261031_064851_d0f106", "card done", display_kind="kanban_notice",
                                        label="kanban-sweep-wake")
    assert verdict == "busy" and touched == []


# ── a build failure on stale code never waits for the wall ────────────────────────────────────────────


def _replay_due(monkeypatch, resets_at: float) -> float:
    from tui_gateway import automation_replay as ar
    from tui_gateway import server

    booked = []
    monkeypatch.setattr(ar, "schedule", lambda key, **k: booked.append(k["due"]) or True)
    session = {"agent_error": "SubscriptionQuotaUnavailable: openai-codex/gpt-6-astra",
               "agent_failure": {"failure_resets_at": resets_at}}
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key: ("sid", session), raising=False)
    ar.replay_failed_wake("20261031_064851_d0f106", "card done", session, "kanban_notice",
                          {"task_id": "t_01a1d628cfa66230"}, "kanban-sweep-wake")
    assert len(booked) == 1
    return booked[0]


def test_a_wall_failure_on_current_code_still_waits_for_the_reset(monkeypatch):
    from gateway import code_skew
    from tui_gateway import notice_guarantee as ng

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)
    now = time.time()
    reset = now + 4 * 86400
    assert _replay_due(monkeypatch, reset) == reset
    assert ng._retry_at("20261031_064851_d0f106", now) == reset + ng.DEFER_SLACK_S


def test_a_skewed_but_not_retired_process_still_waits_for_the_reset(monkeypatch):
    # [daveey] t_01a1d6b19bf73072: this process runs the replay itself, on the same stale code, so a short-delay
    # replay would hit the same wall, fail and give up; only a retired process hands the replay to a successor.
    from gateway import code_skew
    from tui_gateway import automation_replay as ar
    from tui_gateway import notice_guarantee as ng

    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("17f28630e4", "0068f2ca86"))
    assert hll.host_loops_retired() is None
    now = time.time()
    reset = now + 4 * 86400
    assert _replay_due(monkeypatch, reset) == reset
    assert ar.trusted_resets_at(reset) == reset
    assert ng._retry_at("20261031_064851_d0f106", now) == reset + ng.DEFER_SLACK_S


@pytest.mark.parametrize("skewed", [False, True])
def test_a_wall_failure_in_a_retired_process_replays_after_the_short_delay(monkeypatch, skewed):
    from gateway import code_skew
    from tui_gateway import automation_replay as ar
    from tui_gateway import notice_guarantee as ng

    skew = ("17f28630e4", "0068f2ca86") if skewed else None
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: skew)
    hll.retire_host_loops("test", "always-on backend :9148 superseded")
    now = time.time()
    due = _replay_due(monkeypatch, now + 4 * 86400)
    assert now + ar.REPLAY_MIN_DELAY_S <= due <= time.time() + ar.REPLAY_MIN_DELAY_S
    # the durable close-out deferral follows the replay, not the wall
    assert ar.trusted_resets_at(now + 4 * 86400) is None
    assert ng._retry_at("20261031_064851_d0f106", now) == ar.replay_due(now) + ng.DEFER_SLACK_S
