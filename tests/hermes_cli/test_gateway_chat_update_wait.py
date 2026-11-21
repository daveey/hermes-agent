"""[daveey] A finite run (``hermes -z``) started while a core update swaps the gateway waits for the restarted
owner instead of failing with ``Error: Gateway draining: update_paused`` and losing its prompt.

hermes-hub 2026-11-12 17:54Z and 18:02Z: ``hermes -z`` started mid-swap exited at once on that refusal. Every
refusal retried here admitted nothing (ensure's ``draining``/``starting`` verdict before any socket, a draining
owner's ``runtime_draining``/``runtime_retired`` answer to ``session.create``/``prompt.submit``), so the run
asks again, for at most ``ADMISSION_WAIT_S`` from the first refusal, like the running one-shot's redial.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_chat, gateway_update_wait
from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, GatewayUnavailableError

UPDATE_PAUSED = ("Gateway draining: update_paused", "draining", "update_paused")
NOTICE = "waiting up to 10 min"


def _paused():
    return GatewayUnavailableError(*UPDATE_PAUSED)


class Owner:
    """One gateway process: answers the finite run's RPCs; ``refuse`` maps a method to its refusal."""

    def __init__(self, refuse=None, session="s"):
        self.events, self.calls, self.refuse, self.session = asyncio.Queue(), [], dict(refuse or {}), session
        self.closed = False
        self.params = []
        self.closed_at_dial = []

    async def rpc(self, method, **params):
        self.calls.append(method)
        self.params.append((method, params))
        if method in self.refuse:
            raise self.refuse[method]
        if method == "runtime.describe":
            return {"session_create": {"sources": ["cli", "oneshot"], "parameters": ["agent_cli", "hidden", "cwd", "request_id", "source"]}}
        if method in ("session.create", "session.resume"):
            return {"stored_session_id": self.session, "execution_generation": 1, "pending": [], "prompts": []}
        if method == "prompt.submit":
            assert params["session_id"] == self.session and params["text"] == "ping"
            self.events.put_nowait({"method": "event", "params": {
                "type": "message.complete", "session_id": self.session, "admission_id": "a",
                "payload": {"text": "pong", "outcome": "completed"}}})
            return {"admission_id": "a", "status": "queued"}
        if method == "prompt.receipt":
            return {"admission_id": "a", "status": "terminal", "outcome": "completed",
                    "result": {"final_response": "pong", "completed": True}}
        raise AssertionError(method)


@pytest.fixture
def dials(monkeypatch, tmp_path):
    """``connect_gateway`` answering from a script: an exception is that dial's refusal, an Owner attaches."""
    monkeypatch.setattr(gateway_update_wait, "ADMISSION_RETRY_S", 0.01)
    monkeypatch.chdir(tmp_path)
    script, made = [], []

    @asynccontextmanager
    async def connect():
        for owner in made:
            if isinstance(owner, Owner):
                owner.closed_at_dial.append(owner.closed)
        nxt = script.pop(0)
        made.append(nxt)
        if isinstance(nxt, BaseException):
            raise nxt
        try:
            yield nxt
        finally:
            nxt.closed = True

    # Retain generators: GC must not stand in for explicit connection cleanup.
    contexts = []

    def retained_connect():
        context = connect()
        contexts.append(context)
        return context

    monkeypatch.setattr(gateway_chat, "connect_gateway", retained_connect)
    return script, made


async def _z():
    return await asyncio.wait_for(gateway_chat.run_gateway_chat(argparse.Namespace(oneshot="ping")), 10)


@pytest.mark.asyncio
async def test_oneshot_started_during_an_update_waits_for_the_restarted_owner(dials, capsys):
    script, made = dials
    owner = Owner()
    # The fence, the owner coming up, a half-started socket, then the restarted owner.
    script += [_paused(), _paused(), GatewayUnavailableError("Gateway starting: deadline", "starting", "deadline"),
               ConnectionRefusedError("starting"), owner]
    assert await _z() == 0
    assert len(made) == 5 and owner.calls[:3] == ["runtime.describe", "session.create", "prompt.submit"]
    out, err = capsys.readouterr()
    assert "pong" in out
    assert err.count(NOTICE) == 1 and "update_paused" in err


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [
    GatewayUnavailableError("Gateway inaccessible: unsafe_control_node", "inaccessible", "unsafe_control_node"),
    GatewayUnavailableError("Gateway could not start: boom", "inaccessible", "runtime_exited"),
    ConnectionRefusedError("no owner"),  # a failed dial is retried only once an update refusal opened the wait
], ids=["inaccessible", "failed-start", "dial-before-wait"])
async def test_a_refusal_that_is_not_an_update_swap_still_fails_at_once(dials, capsys, refusal):
    script, made = dials
    script += [refusal, Owner()]
    with pytest.raises(type(refusal)):
        await _z()
    assert len(made) == 1 and NOTICE not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_an_owner_that_never_returns_fails_after_the_window(dials, monkeypatch):
    script, made = dials
    # Drive only UpdateWait's clock: a descheduled runner can miss an entire
    # 50ms real-time window after its first retry. Leave asyncio's own clock
    # and _z's watchdog real, and exercise the production ten-minute window.
    now, sleeps = 1000.0, []

    async def advance(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay
        await asyncio.sleep(0)

    loop = SimpleNamespace(time=lambda: now)
    monkeypatch.setattr(gateway_update_wait, "asyncio", SimpleNamespace(
        get_running_loop=lambda: loop, sleep=advance))
    monkeypatch.setattr(gateway_update_wait, "ADMISSION_RETRY_S", 200.0)
    script += [_paused() for _ in range(5)]
    with pytest.raises(GatewayUnavailableError, match="update_paused") as caught:
        await _z()
    # Refusals at t=0, 200, 400 retry; t=600 is exactly the deadline.
    assert len(made) == 4
    assert caught.value is made[-1]
    assert sleeps == [200.0, 200.0, 200.0]
    assert now == 1600.0
    assert len(script) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["runtime_draining", "runtime_retired"])
async def test_a_draining_owner_refusing_the_session_is_left_for_the_next_one(dials, capsys, reason):
    script, made = dials
    draining, restarted = Owner(refuse={"session.create": GatewayRPCError(reason)}), Owner()
    script += [draining, restarted]
    assert await _z() == 0
    assert draining.closed_at_dial == [True]
    assert len(made) == 2 and draining.closed and "prompt.submit" not in draining.calls
    assert restarted.calls[:3] == ["runtime.describe", "session.create", "prompt.submit"]
    assert "pong" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_refused_submit_goes_to_the_restarted_owner_on_the_same_session(dials, capsys):
    script, made = dials
    draining = Owner(refuse={"prompt.submit": GatewayRPCError("runtime_draining")})
    restarted = Owner(refuse={"session.create": AssertionError("a second session")})
    # The next owner is still behind the fence for one dial, then refuses the re-attach once.
    later = Owner(refuse={"session.create": AssertionError("a second session")})
    restarted.refuse["session.resume"] = GatewayRPCError("runtime_draining")
    script += [draining, _paused(), restarted, later]
    assert await _z() == 0
    assert len(made) == 4 and draining.calls.count("session.create") == 1
    assert restarted.calls == ["session.resume"]
    assert later.calls[:2] == ["session.resume", "prompt.submit"]
    assert restarted.closed_at_dial == [True]
    out, err = capsys.readouterr()
    assert "pong" in out and err.count(NOTICE) == 1


@pytest.mark.asyncio
async def test_a_refused_submit_without_a_wait_or_dialer_keeps_the_old_exit():
    from hermes_cli.gateway_chat_view import GatewayChatView
    for wait, reconnect in ((None, Owner), (gateway_update_wait.UpdateWait(), None)):
        owner = Owner(refuse={"prompt.submit": GatewayRPCError("runtime_draining")})
        view = GatewayChatView(owner, {"stored_session_id": "s"}, quiet=True)
        view.update_wait, view.reconnect = wait, reconnect
        with pytest.raises(GatewayRPCError, match="runtime_draining"):
            await asyncio.wait_for(view.run("ping", oneshot=True), 5)
    other = Owner(refuse={"prompt.submit": GatewayRPCError("invalid_params")})
    view = GatewayChatView(other, {"stored_session_id": "s"}, quiet=True)
    view.update_wait, view.reconnect = gateway_update_wait.UpdateWait(), pytest.fail
    with pytest.raises(GatewayRPCError, match="invalid_params"):
        await asyncio.wait_for(view.run("ping", oneshot=True), 5)


@pytest.mark.asyncio
async def test_stream_json_waits_without_a_stderr_notice(monkeypatch, capsys):
    monkeypatch.setattr(gateway_update_wait, "ADMISSION_RETRY_S", 0)
    wait = gateway_update_wait.UpdateWait(announce=False)
    await wait.retry(_paused())
    assert wait.waiting and wait.retries == 1 and capsys.readouterr().err == ""
    with pytest.raises(GatewayClientError, match="session_replay_gap"):
        await wait.retry(GatewayClientError("session_replay_gap"))


@pytest.mark.asyncio
async def test_create_retry_reuses_request_id_but_a_new_run_does_not(dials):
    script, _ = dials
    retired = Owner(refuse={"session.create": GatewayRPCError("runtime_retired")})
    restarted, another_run = Owner(), Owner()
    script += [retired, restarted, another_run]
    assert await _z() == 0
    assert await _z() == 0
    ids = [next(params["request_id"] for method, params in owner.params if method == "session.create")
           for owner in (retired, restarted, another_run)]
    assert ids[0] == ids[1]
    assert ids[1] != ids[2]


@pytest.mark.asyncio
async def test_submit_retry_reuses_input_id_but_a_new_run_does_not(dials):
    script, _ = dials
    retired = Owner(refuse={"prompt.submit": GatewayRPCError("runtime_retired")})
    restarted, another_run = Owner(), Owner()
    script += [retired, restarted, another_run]
    assert await _z() == 0
    assert await _z() == 0
    ids = [next(params["input_id"] for method, params in owner.params if method == "prompt.submit")
           for owner in (retired, restarted, another_run)]
    assert ids[0] == ids[1]
    assert ids[1] != ids[2]


@pytest.mark.asyncio
async def test_admission_notice_does_not_claim_an_update(monkeypatch, capsys):
    monkeypatch.setattr(gateway_update_wait, "ADMISSION_RETRY_S", 0)
    wait = gateway_update_wait.UpdateWait()
    await wait.retry(GatewayRPCError("runtime_draining"))
    await wait.retry(GatewayRPCError("runtime_draining"))
    notice = capsys.readouterr().err
    assert notice.count(NOTICE) == 1
    assert "not admitting new turns yet" in notice
    assert "restarting for an update" not in notice
