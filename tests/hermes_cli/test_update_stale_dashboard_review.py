"""``hermes update`` must not SIGTERM a stale dashboard backend mid background review.

The update's dashboard step (``_kill_stale_dashboard_processes(restart_managed=True)``) used to send a
plain SIGTERM. On SIGTERM the serve finalizes its sessions and ``tui_gateway.session_lifecycle``
calls ``cancel_background_review_for_live_turn``, which cuts a running review off mid provider call.
The drain window applies an update at the first idle gap, and a review starts right after every
turn, so a review is often in flight at that moment (David-MBP 2026-10-28 06:37:41, pid 91625).

The stop now goes through the backend's own retirement fence (``POST /api/health/retirement``): a
running review keeps it refused, past ``BACKGROUND_REVIEW_WAIT_S`` the fence asks the review to stop
before its next provider call, and the update stops the backend only once the fence commits (or at
the review hard cap, so the update never blocks past the existing 300/900 s bounds).

The backend here is a real child process: a real ``RetirementFence`` behind a real HTTP listener
authenticated by ``HERMES_DASHBOARD_SESSION_TOKEN`` (read from the child's environment, as the update
does for a Desktop-local backend), a review thread that makes "provider calls" and honours
``wind_down`` only between calls, and a SIGTERM handler that records what the real shutdown would do
to a review still running (cancel it; mid-call when a call is in flight).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import dashboard_procs, main_dashboard

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and /proc environ")

_BACKEND = textwrap.dedent(
    """
    import json, os, pathlib, signal, sys, threading, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from hermes_cli import backend_retirement, web_server_idle_proof

    out = pathlib.Path(sys.argv[1])
    calls, call_s, wait_s = int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    backend_retirement.BACKGROUND_REVIEW_WAIT_S = wait_s
    # No turns, no human input: only the review can hold this backend.
    web_server_idle_proof.idle_proof = lambda: {"idle": True, "reason": None}
    fence = backend_retirement.retirement
    token = os.environ["HERMES_DASHBOARD_SESSION_TOKEN"]
    state = {"in_call": False, "running": True, "events": []}

    log_lock = threading.Lock()

    def log(event):
        # Review thread, HTTP handler threads and the SIGTERM handler all log: serialize the
        # append+write (no lost update) and publish atomically (the test never reads a torn file).
        with log_lock:
            state["events"].append(event)
            path = out.with_suffix(".events")
            tmp = path.with_suffix(".events.tmp")
            tmp.write_text(json.dumps(state["events"]))
            os.replace(tmp, path)

    hold = fence.begin_background_review() if calls else None

    def review():
        try:
            for n in range(calls):
                if hold.wind_down.is_set():  # begin_iteration: the call boundary
                    log("wound_down_at_call_boundary")
                    return
                state["in_call"] = True
                log(f"call_{n}_start")
                time.sleep(call_s)
                state["in_call"] = False
                log(f"call_{n}_end")
            log("review_completed")
        finally:
            state["running"] = False
            fence.end_background_review(hold)

    if hold is not None:
        threading.Thread(target=review, daemon=True).start()
    else:
        state["running"] = False

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            if self.headers.get("X-Hermes-Session-Token") != token:
                self._reply({"detail": "Unauthorized"}, 401)
                return False
            return True

        def do_GET(self):  # /api/health/idle, shaped like busy_ledger's report
            if not self._authed():
                return
            n = fence.background_review_count()
            self._reply({"ok": True, "idle": not n, "reason": "turn_in_flight" if n else None,
                         "detail": f"background_review:{n}" if n else None})

        def do_POST(self):  # /api/health/retirement
            if not self._authed():
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            action = body.get("action")
            if action == "prepare":
                reply = fence.prepare()
            else:
                reply = getattr(fence, action)(body.get("token"))
            log(f"{action}:{'ok' if reply.get('ok') else 'refused'}")
            self._reply(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def on_term(*_):
        # tui_gateway.session_lifecycle: shutdown finalizes sessions and cancels any review that is
        # still running (cancel_background_review_for_live_turn), mid-call if a call is in flight.
        if state["running"]:
            log("review_cancelled_mid_call" if state["in_call"] else "review_cancelled_between_calls")
        else:
            log("sigterm_no_review_running")
        out.write_text("done")
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    # Existence is the parent's ready signal; publish only after the port is fully written.
    port = out.with_suffix(".port")
    tmp = port.with_suffix(".port.tmp")
    tmp.write_text(str(server.server_address[1]))
    os.replace(tmp, port)
    time.sleep(600)
    """
)


def _spawn_backend(tmp_path: Path, *, calls: int, call_s: float, wait_s: float = 300.0):
    out = tmp_path / "backend.out"
    env = dict(os.environ, HERMES_DASHBOARD_SESSION_TOKEN="tok-" + os.urandom(8).hex(),
               PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parents[2]),
                                           os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.Popen([sys.executable, "-c", _BACKEND, str(out), str(calls), str(call_s), str(wait_s)],
                            env=env, stdin=subprocess.DEVNULL)
    port_file = out.with_suffix(".port")
    deadline = time.monotonic() + 30
    while not port_file.exists():
        assert proc.poll() is None and time.monotonic() < deadline, "backend stand-in did not start"
        time.sleep(0.02)
    return proc, out, int(port_file.read_text())


def test_backend_port_is_not_visible_until_its_contents_are_complete(tmp_path):
    """Pause the real child's first write after open, before any port bytes reach disk."""
    from tests.waiting import wait_until

    opened_r, opened_w = os.pipe()
    resume_r, resume_w = os.pipe()
    paused_write = textwrap.dedent(
        """
        import os, pathlib, sys
        def paused_write_text(self, data, *args, **kwargs):
            with self.open("w") as stream:
                os.write(int(sys.argv[5]), b"opened")
                assert os.read(int(sys.argv[6]), 1) == b"x"
                return stream.write(data)
        pathlib.Path.write_text = paused_write_text
        """
    )
    out = tmp_path / "backend.out"
    port_file = out.with_suffix(".port")
    env = dict(os.environ, HERMES_DASHBOARD_SESSION_TOKEN="publication-test-token",
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", paused_write + _BACKEND, str(out), "0", "0", "300",
             str(opened_w), str(resume_r)],
            env=env, stdin=subprocess.DEVNULL, pass_fds=(opened_w, resume_r),
        )
        assert select.select([opened_r], [], [], 30)[0], "backend never began publishing its port"
        assert os.read(opened_r, 6) == b"opened"
        # Existence is the parent's ready signal: it must not expose the empty staging write.
        assert not port_file.exists()
        os.write(resume_w, b"x")
        assert wait_until(port_file.exists, timeout=10)
        assert 0 < int(port_file.read_text()) < 65536
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=10)
        for fd in (opened_r, opened_w, resume_r, resume_w):
            os.close(fd)


def _events(out: Path) -> list[str]:
    path = out.with_suffix(".events")
    return json.loads(path.read_text()) if path.exists() else []


def _run_update_stop(pid: int, port: int):
    """The update's dashboard step on one stale, manually started backend (nothing to respawn)."""
    with patch.object(main_dashboard, "_restart_managed_dashboard_service", return_value=False), \
         patch.object(main_dashboard, "_find_stale_dashboard_pids", return_value=[pid]), \
         patch.object(main_dashboard, "_get_pid_cgroup_path", return_value=None), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None), \
         patch.object(main_dashboard, "_dashboard_cmdline_for_pid", return_value=None), \
         patch.object(main_dashboard, "_loaded_launchd_backend_jobs", return_value=[]), \
         patch.object(main_dashboard, "_launchd_job_from_launch_env", return_value=None), \
         patch.object(dashboard_procs, "_lock_owned_serve_pids", return_value=set()), \
         patch.object(dashboard_procs, "_launcher_supervisor", return_value=None), \
         patch.object(dashboard_procs, "_ledger_serve_binds", return_value={pid: ("127.0.0.1", port)}):
        started = time.monotonic()
        result = dashboard_procs._kill_stale_dashboard_processes(restart_managed=True)
        return result, time.monotonic() - started


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(dashboard_procs, "_REVIEW_RETIRE_POLL_S", 0.1, raising=False)


def test_stale_backend_mid_review_is_stopped_only_after_the_review_finishes(tmp_path, fast_poll):
    proc, out, port = _spawn_backend(tmp_path, calls=3, call_s=0.7)
    try:
        deadline = time.monotonic() + 10
        while "call_0_start" not in _events(out):  # the review is mid provider call
            assert time.monotonic() < deadline
            time.sleep(0.02)
        result, _elapsed = _run_update_stop(proc.pid, port)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    events = _events(out)
    assert "review_cancelled_mid_call" not in events, events
    assert "review_cancelled_between_calls" not in events, events
    assert "review_completed" in events, events
    # Stopped through the fence: committed after the review, then SIGTERMed with nothing running.
    assert events.index("review_completed") < events.index("commit:ok") < events.index(
        "sigterm_no_review_running"), events
    assert result["killed"] == [proc.pid] and result["failed"] == []


def test_review_past_the_wait_bound_winds_down_at_a_call_boundary(tmp_path, fast_poll):
    # A long review (20 calls of 0.5 s) on a backend whose fence asks it to wind down after 0.3 s.
    proc, out, port = _spawn_backend(tmp_path, calls=20, call_s=0.5, wait_s=0.3)
    try:
        deadline = time.monotonic() + 10
        while "call_0_start" not in _events(out):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        result, elapsed = _run_update_stop(proc.pid, port)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    events = _events(out)
    assert "review_cancelled_mid_call" not in events, events
    assert "wound_down_at_call_boundary" in events, events
    assert "review_completed" not in events, events  # it did not need to run all 20 calls
    assert elapsed < 8, elapsed
    assert events.index("wound_down_at_call_boundary") < events.index("commit:ok") < events.index(
        "sigterm_no_review_running"), events
    assert result["killed"] == [proc.pid]


def test_update_does_not_wait_past_the_review_hard_cap(tmp_path, fast_poll, monkeypatch):
    # A wedged review that never reaches a call boundary: the update gives up at its deadline and
    # stops the backend as before, rather than blocking the update forever.
    monkeypatch.setattr(dashboard_procs, "_review_retire_deadline_s", lambda: 1.0, raising=False)
    proc, out, port = _spawn_backend(tmp_path, calls=1, call_s=60.0)
    try:
        deadline = time.monotonic() + 10
        while "call_0_start" not in _events(out):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        result, elapsed = _run_update_stop(proc.pid, port)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert 1.0 <= elapsed < 15, elapsed
    assert result["killed"] == [proc.pid]
    assert "prepare:refused" in _events(out)  # it did ask the fence first


def test_control_backend_with_no_review_is_stopped_as_today(tmp_path, fast_poll):
    proc, out, port = _spawn_backend(tmp_path, calls=0, call_s=0.0)
    try:
        result, elapsed = _run_update_stop(proc.pid, port)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert _events(out)[-1] == "sigterm_no_review_running"
    assert result["killed"] == [proc.pid] and result["failed"] == []
    assert elapsed < 5, elapsed


def test_control_backend_without_a_reachable_fence_is_stopped_as_today(tmp_path, fast_poll):
    # Old backend / unknown port: no retirement endpoint, so the update falls back to the plain stop.
    proc, out, port = _spawn_backend(tmp_path, calls=3, call_s=5.0)
    try:
        with patch.object(dashboard_procs, "_retirement_endpoint", return_value=None, create=True):
            deadline = time.monotonic() + 10
            while "call_0_start" not in _events(out):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            result, elapsed = _run_update_stop(proc.pid, port)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert result["killed"] == [proc.pid]
    assert elapsed < 5, elapsed


def test_review_ending_between_prepare_and_idle_probe_still_commits_the_fence(monkeypatch, fast_poll):
    """The review can end between the update's refused ``prepare`` and its idle probe. That idle
    answer must not turn into an unfenced stop: the update re-prepares and commits, so no new
    turn or review can start between the review's end and the SIGTERM (scripted, no timing)."""
    endpoint = ("127.0.0.1", 1, "tok")
    answers = iter([
        ({"action": "prepare"}, {"ok": False, "idle": False}),   # review running
        (None, {"ok": True, "idle": False, "detail": "background_review:1"}),
        ({"action": "prepare"}, {"ok": False, "idle": False}),   # review still running ...
        (None, {"ok": True, "idle": True, "detail": None}),       # ... and ends right here
        ({"action": "prepare"}, {"ok": True, "idle": True, "token": "permit"}),
        ({"action": "commit", "token": "permit"}, {"ok": True}),
    ])
    seen = []

    def scripted(ep, body):
        assert ep == endpoint
        expected, reply = next(answers)
        assert body == expected, (body, expected)
        seen.append(body)
        return reply

    monkeypatch.setattr(dashboard_procs, "_retirement_endpoint", lambda pid: endpoint)
    monkeypatch.setattr(dashboard_procs, "_retirement_request", scripted)
    dashboard_procs._await_background_reviews([4242])
    assert seen[-1] == {"action": "commit", "token": "permit"}, seen


def test_a_backend_that_stays_unpreparable_after_its_review_is_stopped_as_before(monkeypatch, fast_poll):
    """The re-prepare is one retry, not a loop: a fence that keeps refusing while the backend reports
    something other than a review is stopped the old way (no 900 s hang)."""
    endpoint = ("127.0.0.1", 1, "tok")
    answers = iter([
        ({"action": "prepare"}, {"ok": False, "idle": False}),
        (None, {"ok": True, "idle": False, "detail": "background_review:1"}),
        ({"action": "prepare"}, {"ok": False, "idle": False}),
        (None, {"ok": True, "idle": False, "detail": "turn_in_flight"}),
        ({"action": "prepare"}, {"ok": False, "idle": False}),
        (None, {"ok": True, "idle": False, "detail": "turn_in_flight"}),
    ])
    seen = []

    def scripted(ep, body):
        expected, reply = next(answers)
        assert body == expected, (body, expected)
        seen.append(body)
        return reply

    monkeypatch.setattr(dashboard_procs, "_retirement_endpoint", lambda pid: endpoint)
    monkeypatch.setattr(dashboard_procs, "_retirement_request", scripted)
    dashboard_procs._await_background_reviews([4242])
    assert len(seen) == 6 and {"action": "commit", "token": "permit"} not in seen


def test_unanswered_idle_probe_while_waiting_is_not_taken_for_the_review_ending(monkeypatch, fast_poll):
    """A starved backend can miss the HTTP timeout; no answer must keep the wait, not stop unfenced."""
    endpoint = ("127.0.0.1", 1, "tok")
    answers = iter([
        ({"action": "prepare"}, {"ok": False, "idle": False}),
        (None, {"ok": True, "idle": False, "detail": "background_review:1"}),
        ({"action": "prepare"}, {"ok": False, "idle": False}),
        (None, None),                                             # probe timed out
        ({"action": "prepare"}, None),                            # so did the prepare
        (None, None),
        ({"action": "prepare"}, {"ok": True, "idle": True, "token": "permit"}),
        ({"action": "commit", "token": "permit"}, {"ok": True}),
    ])
    seen = []

    def scripted(ep, body):
        expected, reply = next(answers)
        assert body == expected, (body, expected)
        seen.append(body)
        return reply

    monkeypatch.setattr(dashboard_procs, "_retirement_endpoint", lambda pid: endpoint)
    monkeypatch.setattr(dashboard_procs, "_retirement_request", scripted)
    monkeypatch.setattr(dashboard_procs, "_pid_alive", lambda pid: True)
    dashboard_procs._await_background_reviews([4242])
    assert seen[-1] == {"action": "commit", "token": "permit"}, seen
