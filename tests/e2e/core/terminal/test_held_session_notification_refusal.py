"""A chat held by another process must not get a refused notification turn re-run in a loop.

Live repro of 2026-10-28 (David-MBP desktop.log, chat 20261023_204423_b85a94): a background-process
completion was waiting in the Desktop backend while another device's backend held the chat. The poller
re-ran the completion turn every ~0.8 s; each one was refused SESSION_NOT_OWNED and painted an
"internal error ... Your message was not answered" card (139 in 110 s).

Harness: the REAL ``hermes serve`` backend (Desktop's argv) with a fake LLM, one Desktop-shaped
WebSocket client. The client's turn starts ``terminal(background=true, notify_on_complete=true)`` and
keeps the chat open. A SECOND OS process then takes the chat's active-session lease over and keeps it
(another device's backend holding it). When the background job finishes, its completion turn is refused. Asserts at
most 3 attempts (message.start) and exactly one error notice over the window, with owner copy, not
"internal error".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from tests.e2e.core.terminal._gateway_client import REPO_ROOT, Backend, etype, poll_until
from tests.fakes.fake_llm_provider import Text, ToolCall

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group lifecycle")

WINDOW_S = float(os.environ.get("HELD_REFUSAL_WINDOW_S", "45"))
JOB_S = 12

_HOLDER = r"""
import os, sys, time
from hermes_state import SessionDB
from hermes_state_pidns import holder_namespace_token
# Another device's backend on the same host shares state.db and holds the chat's durable turn lease
# (2026-10-28: the mini's Desktop backend, SSH-spawned on David-MBP). The Desktop backend's admission reads
# that lease (_durable_owner_refusal) and refuses SESSION_NOT_OWNED, the incident's refusal.
holder = f"pid={os.getpid()}{holder_namespace_token()}:turn=other-device:platform=desktop"
db = SessionDB()
ok = db.try_acquire_session_turn_lease(sys.argv[1], holder, ttl_seconds=600)
print("HELD" if ok else "REFUSED", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _responder(record: dict):
    messages = record["body"].get("messages") or []
    tool_results = [m for m in messages if m.get("role") == "tool"]
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    text = str(last_user.get("content") or "")
    if "start the job" in text and not tool_results:
        return ToolCall("terminal", {"command": f"sleep {JOB_S} && echo bgdone", "background": True,
                                     "notify_on_complete": True})
    return Text("ok, noted")


@pytest.fixture
def backend(tmp_path):
    be = Backend(tmp_path, _responder)
    be.start()
    try:
        yield be
    finally:
        be.stop()


def test_refused_completion_turn_is_bounded_and_noticed_once(backend):
    client = backend.connect("desktop")
    sid = client.call("session.create", cols=80)["session_id"]
    start = len(client.snapshot())
    client.call("prompt.submit", session_id=sid, text="please start the job")
    client.wait_for(lambda f: etype(f) == "message.complete" and f["params"].get("session_id") == sid,
                    timeout=90, start=start, what="first turn complete")
    job_started = time.monotonic()
    key = poll_until(lambda: (backend.db_rows("select id from sessions order by started_at desc limit 1")
                              or [[None]])[0][0], timeout=30, what="session row")

    env = {**os.environ, "HOME": str(backend.home), "HERMES_HOME": str(backend.hermes_home),
           "PYTHONPATH": str(REPO_ROOT), "HERMES_STATE_DB_GUARD_BYPASS": "1"}
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, key, str(WINDOW_S + JOB_S + 30)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stderr is not None
        line = poll_until(lambda: holder.stdout.readline().strip(), timeout=60, what="holder lease")
        assert line.startswith("HELD"), f"holder could not take the lease: {line} {holder.stderr.read()}"
        rsid = sid
        assert time.monotonic() - job_started < JOB_S, "set up too slowly: the job finished before the hold"
        mark = len(client.snapshot())
        held_at = time.monotonic()
        time.sleep(WINDOW_S)
        frames = [f for f in client.snapshot()[mark:] if f.get("method") == "event"
                  and f["params"].get("session_id") == rsid]
    finally:
        holder.kill()
        holder.wait(timeout=10)

    starts = [f for f in frames if etype(f) == "message.start"]
    errors = [f["params"]["payload"] for f in frames if etype(f) == "message.complete"
              and (f["params"].get("payload") or {}).get("status") == "error"]
    report = {
        "session_key": key, "window_s": WINDOW_S,
        "attempts_message_start": len(starts),
        "error_notices": len(errors),
        "first_start_after_hold_s": round(starts[0]["_t"] - held_at, 2) if starts else None,
        "notice_codes": [(e.get("error_surface") or {}).get("code") for e in errors],
        "notice_first_line": [str(e.get("text") or "").splitlines()[0] for e in errors[:3]],
    }
    print("REPRO " + json.dumps(report))
    seen = [(etype(f), (f.get("params") or {}).get("session_id")) for f in client.snapshot()
            if f.get("method") == "event"]
    assert errors, (f"the completion turn never ran against the held chat (sid={sid} rsid={rsid}):\n"
                    f"events={seen}\nllm={[str(b.get('messages', [])[-1:])[:300] for b in backend.llm.main_requests()]}\n"
                    f"{backend.logs()}")
    assert len(starts) <= 3, report
    assert len(errors) == 1, report
    assert report["notice_codes"] == ["SESSION_NOT_OWNED"], report
    assert "internal error" not in errors[0]["text"], report
