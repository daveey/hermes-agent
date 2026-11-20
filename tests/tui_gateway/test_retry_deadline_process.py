"""Opt-in fresh-interpreter proof; never contacts a provider or a live home.

Run this file separately with -k pause|repeat|resume and
--basetemp <disposable-dir>/retry-proof-<phase>; all phases share proof-data
beside those pytest temporary roots. Uses real 503 Retry-After 600 and production
pause timing. The resume phase intentionally takes the unexpired ten minutes.
"""
import json
import os
from pathlib import Path
import threading
import time

import pytest

from hermes_cli import admission_fence as af
from tui_gateway import server, update_pause as up
from tui_gateway.turn_marker import read_turn_marker
from tests.tui_gateway import test_retry_deadline_gateway as gw

@pytest.fixture()
def proof_home(request):
    base = request.config.getoption("basetemp")
    if not base or not Path(base).name.startswith("retry-proof-"):
        pytest.skip("explicit --basetemp .../retry-proof-* process proof only")
    path = Path(base).parent / "proof-data"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def rig(proof_home, monkeypatch):
    yield from gw.rig.__wrapped__(proof_home, monkeypatch)


@pytest.mark.parametrize("MODE", ["pause", "repeat", "resume"])
def test_fresh_process_retry_deadline(rig, monkeypatch, MODE):
    home, make = rig
    # Restore exactly the production constants the ordinary integration fixture shortens.
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 1.0)
    monkeypatch.setattr(gw, "RETRY_AFTER_S", 600.0)
    state_path = home / "proof-state.json"
    agent = make(lambda *a, **k: gw.base._response())
    assert getattr(agent, "_credential_pool", None) is None
    calls = []
    observed = threading.Event()
    original_wait = None
    if MODE == "pause":
        provider = gw._Provider()
        agent.client.chat.completions.create.side_effect = provider
        session = gw._session(agent, home)
        assert read_turn_marker(home, gw.KEY) is None

        def issue():
            if provider.failed.wait(20):
                af.set_hold(home)
                af.request_pause(home, expires_at=time.time() + 120)

        issuer = threading.Thread(target=issue)
        issuer.start()
        try:
            server._run_prompt_submit("proof", "proof-sid", session, "run the quick job")
            gw._join_turn(session, 75)
        finally:
            # Bound RED on old code without allowing an ordinary 600s wait to leak.
            if session.get("_run_thread") and session["_run_thread"].is_alive():
                agent.interrupt()
                session["_run_thread"].join(15)
            issuer.join(25)
        marker = read_turn_marker(home, gw.KEY)
        assert marker and marker["reason"] == up.PAUSE_REASON, marker
        deadline = marker.get("provider_retry_not_before")
        assert deadline is not None, "pre-fix drops Retry-After at pause"
        assert provider.failed_at + 599 <= deadline <= provider.failed_at + 602
        assert len(provider.times) == 2
        state = {"deadline": deadline, "failed_at": provider.failed_at, "pause_pid": os.getpid(),
                 "pause_at": time.time(), "pauses": marker["pauses"]}
        state_path.write_text(json.dumps(state), encoding="utf-8")
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert os.getpid() != state["pause_pid"]
        deadline = state["deadline"]
        marker = read_turn_marker(home, gw.KEY)
        assert marker and marker["provider_retry_not_before"] == deadline
        assert time.time() < deadline - 10, "must exercise an actually unexpired deadline"
        af.clear_pause_request(home)
        af.clear_hold(home)

        def provider(*a, **k):
            calls.append(time.time())
            return gw.base._response()

        agent.client.chat.completions.create.side_effect = provider
        fresh = gw._session(agent, home)
        fresh["running"] = False
        monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
        monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
        issuer = None
        if MODE == "repeat":
            # Request only after the real gateway installed the deadline and entered
            # provider work. Do not manually install deadline callbacks.
            from agent import provider_retry_deadline as prd
            original_wait = prd._sleep

            def sleep(seconds):
                observed.set()
                original_wait(seconds)

            monkeypatch.setattr(prd, "_sleep", sleep)

            def issue():
                if observed.wait(20):
                    af.set_hold(home)
                    af.request_pause(home, expires_at=time.time() + 120)

            issuer = threading.Thread(target=issue)
            issuer.start()
        started = time.time()
        try:
            assert server._maybe_schedule_auto_continue("proof-resume", fresh, gw.KEY) is not None
            gw._join_scheduler(660)
            gw._join_turn(fresh, 660)
        finally:
            if issuer:
                issuer.join(25)
        if MODE == "repeat":
            marker = read_turn_marker(home, gw.KEY)
            assert observed.is_set() and not calls
            assert marker["provider_retry_not_before"] == deadline
            assert marker["pauses"] == state["pauses"] + 1
            state.update(repeat_pid=os.getpid(), repeat_at=time.time())
        else:
            assert MODE == "resume"
            assert len(calls) == 1 and calls[0] >= deadline, (calls, deadline)
            assert calls[0] - deadline < 15
            assert read_turn_marker(home, gw.KEY) is None
            state.update(resume_pid=os.getpid(), resume_started=started, provider_at=calls[0],
                         resume_at=time.time(), no_early_provider=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    print("PROCESS_RETRY_PROOF", json.dumps(state, sort_keys=True), flush=True)
