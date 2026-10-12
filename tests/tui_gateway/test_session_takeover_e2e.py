"""End-to-end "Take over here" across TWO real gateway processes sharing one HERMES_HOME.

Process A imports ``tui_gateway.server`` (revocation watcher running), claims the lease for a
throwaway session X and idles like an open window. Process B imports its own ``tui_gateway.server``
and calls the ``session.take_over`` handler for X. No model is ever called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KEY = "e2e-takeover-session"

_COMMON = """
import json, os, sys, threading, time
from pathlib import Path
from tui_gateway import server
from hermes_cli.active_sessions import SESSION_NOT_OWNED

def make_session():
    return {"agent": None, "session_key": os.environ["KEY"], "history": [], "history_lock": threading.Lock(),
            "history_version": 0, "running": False, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "source": "desktop"}

def report(name, payload):
    path = Path(os.environ["OUT_DIR"]) / name
    path.with_suffix(".tmp").write_text(json.dumps(payload))
    path.with_suffix(".tmp").rename(path)
"""

_OWNER_A = _COMMON + """
sid = "window-a"
session = make_session()
server._sessions[sid] = session
refusal = server._ensure_active_session_slot(sid, session)
assert refusal is None, refusal
lease = session["active_session_lease"]
report("a_ready.json", {"pid": os.getpid(), "lease_id": lease.lease_id})
deadline = time.monotonic() + 60
while not session.get("_lease_taken_over") and time.monotonic() < deadline:
    time.sleep(0.05)
taken_at = time.monotonic()
reclaim = server._ensure_active_session_slot(sid, session)
report("a_after.json", {
    "taken_over": bool(session.get("_lease_taken_over")),
    "lease_released": lease.released,
    "reclaim_reason": getattr(reclaim, "reason", None),
    "reclaim_message": str(reclaim) if reclaim is not None else None,
})
Path(os.environ["OUT_DIR"], "a_done").touch()
deadline = time.monotonic() + 60
while not Path(os.environ["OUT_DIR"], "stop").exists() and time.monotonic() < deadline:
    time.sleep(0.05)
"""

_TAKER_B = _COMMON + """
sid = "window-b"
session = make_session()
server._sessions[sid] = session
before = server._ensure_active_session_slot(sid, session)
started = time.monotonic()
resp = server._methods["session.take_over"]("rpc-1", {"session_id": sid})
elapsed = time.monotonic() - started
lease = session.get("active_session_lease")
again = server._methods["session.take_over"]("rpc-2", {"session_id": sid})
report("b_result.json", {
    "pid": os.getpid(), "refused_before": getattr(before, "reason", None), "response": resp,
    "elapsed_s": elapsed, "lease_id": getattr(lease, "lease_id", None), "second": again,
})
"""


def _spawn(script: str, *, home: Path, out: Path, watcher: bool) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if not (k.endswith("_API_KEY") or k.endswith("_TOKEN"))}
    env.update({
        "HERMES_HOME": str(home), "OUT_DIR": str(out), "KEY": KEY,
        "PYTHONPATH": os.pathsep.join(p for p in (str(REPO), env.get("PYTHONPATH", "")) if p),
        "HERMES_SESSION_TAKEOVER_POLL_S": "0.2",
    })
    if watcher:
        # The harness marker keeps the background watcher off in in-process tests; A is a real backend.
        env.pop("HERMES_TEST_ISOLATION", None)
    return subprocess.Popen([sys.executable, "-c", script], env=env, cwd=str(REPO),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_file(path: Path, proc: subprocess.Popen[str], timeout: float = 90.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"{path.name}: process exited early rc={proc.returncode}\nstdout={out[-3000:]}\nstderr={err[-6000:]}")
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {path.name}")
        time.sleep(0.05)
    return json.loads(path.read_text()) if path.suffix == ".json" else None


def test_take_over_between_two_real_gateway_processes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    a = _spawn(_OWNER_A, home=home, out=out, watcher=True)
    b = None
    try:
        a_ready = _wait_file(out / "a_ready.json", a)
        registry = json.loads((home / "runtime" / "active_sessions.json").read_text())["entries"]
        assert [(e["session_id"], e["pid"]) for e in registry] == [(KEY, a_ready["pid"])]

        b = _spawn(_TAKER_B, home=home, out=out, watcher=False)
        b_result = _wait_file(out / "b_result.json", b)
        a_after = _wait_file(out / "a_after.json", a)

        print("\nA ready:", json.dumps(a_ready))
        print("B result:", json.dumps(b_result, indent=1))
        print("A after:", json.dumps(a_after))

        assert b_result["refused_before"] == "SESSION_NOT_OWNED"
        result = b_result["response"]["result"]
        assert result["status"] == "taken_over"
        assert result["acknowledged"] is True
        assert result["previous_owner"]["pid"] == a_ready["pid"]
        assert result["previous_owner"]["surface"] == "desktop"
        assert b_result["elapsed_s"] < 5.0
        assert b_result["second"]["result"]["status"] == "already_owner"

        assert a_after == {"taken_over": True, "lease_released": True, "reclaim_reason": "SESSION_NOT_OWNED",
                           "reclaim_message": a_after["reclaim_message"]}

        registry = json.loads((home / "runtime" / "active_sessions.json").read_text())["entries"]
        print("registry:", json.dumps(registry, indent=1))
        assert len(registry) == 1
        (entry,) = registry
        assert entry["pid"] == b_result["pid"] and entry["lease_id"] == b_result["lease_id"]
        assert entry["metadata"]["took_over_from"] == [a_ready["lease_id"]]
        assert entry["metadata"]["handoff_acked"] == [a_ready["lease_id"]]

        (out / "stop").touch()
        a_stdout, _a_err = a.communicate(timeout=30)
        frames = [json.loads(line) for line in a_stdout.splitlines() if line.startswith("{")]
        errors = [f["params"] for f in frames if f.get("method") == "event" and f["params"].get("type") == "error"]
        print("A error events:", json.dumps(errors))
        assert any(e.get("session_id") == "window-a"
                   and e["payload"]["message"] == "This chat was taken over by another Hermes window."
                   and e["payload"]["error_surface"]["code"] == "SESSION_NOT_OWNED" for e in errors)
    finally:
        (out / "stop").touch()
        for proc in (a, b):
            if proc is not None and proc.poll() is None:
                proc.kill()
            if proc is not None:
                proc.communicate()
