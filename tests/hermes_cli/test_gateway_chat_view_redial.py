"""[daveey] A finite run (`chat -q` / `-z`, the hub's `oneshot` reviewer chats) outlives a core update.

The update parks the turn on a durable checkpoint and restarts the gateway; the next owner continues the same
admission. The view used to exit on the hang-up ("turn outcome is unknown") while its turn went on; it now
dials the replacement and waits for that admission's live completion or settled receipt. Anything other than
a hang-up, a view without a dialer, or a replacement that never comes keeps the old exit.
"""
import asyncio

import pytest

from hermes_cli import gateway_chat_view as gcv
from hermes_cli.gateway_client import DISCONNECTED, GatewayClientError


def _event(kind, payload, admission="a"):
    return {"method": "event", "params": {"type": kind, "session_id": "s", "admission_id": admission,
                                          "payload": payload}}


class Life:
    """One gateway process as the view sees it."""

    def __init__(self, answer):
        self.events, self.answer, self.calls = asyncio.Queue(), answer, []

    async def rpc(self, method, **params):
        self.calls.append(method)
        return await self.answer(self, method, params)


async def _first(life, method, params):
    if method == "prompt.submit":
        life.events.put_nowait(_event("message.delta", {"text": "Work"}))
        life.events.put_nowait(GatewayClientError(DISCONNECTED))  # the update restarts the owner
        return {"admission_id": "a", "status": "queued"}
    raise AssertionError(method)


def _view(first, dials):
    view = gcv.GatewayChatView(Life(first), {"stored_session_id": "s"}, quiet=True)

    async def reconnect():
        nxt = dials.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    view.reconnect = reconnect
    return view


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False], ids=["completes-live", "settled-before-redial"])
async def test_oneshot_redials_the_restarted_owner_and_prints_the_continued_answer(capsys, monkeypatch, live):
    monkeypatch.setattr(gcv, "REDIAL_RETRY_S", 0.01)

    async def second(life, method, params):
        if method == "session.resume":
            if live:  # parked, continued by the new owner: its completion follows on this socket
                life.events.put_nowait(_event("message.complete", {"text": "Work done", "outcome": "completed"}))
                return {"pending": [{"admission_id": "a", "status": "started"}], "prompts": []}
            return {"pending": [], "prompts": []}  # ended before this dial: its receipt settles it
        if method == "prompt.receipt":
            return {"admission_id": "a", "status": "terminal", "outcome": "completed",
                    "result": {"final_response": "Work done", "completed": True}}
        raise AssertionError(method)

    replacement = Life(second)
    view = _view(_first, [ConnectionRefusedError("starting"), replacement])
    assert await asyncio.wait_for(view.run("review the PR", oneshot=True), 10) == 0
    assert view.redials == 1 and "session.resume" in replacement.calls
    assert "Work done" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_oneshot_without_a_dialer_or_past_the_window_keeps_the_unknown_exit(monkeypatch):
    monkeypatch.setattr(gcv, "REDIAL_RETRY_S", 0.01)
    monkeypatch.setattr(gcv, "REDIAL_WINDOW_S", 0.05)
    view = _view(_first, [])
    view.reconnect = None
    with pytest.raises(GatewayClientError, match="outcome is unknown"):
        await asyncio.wait_for(view.run("q", oneshot=True), 5)
    assert view.redials == 0

    never = _view(_first, [ConnectionRefusedError("down")] * 1000)
    with pytest.raises(GatewayClientError, match="outcome is unknown"):
        await asyncio.wait_for(never.run("q", oneshot=True), 5)
    assert never.redials == 1


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_a_hang_up_never_redials():
    async def refused(life, method, params):
        if method == "prompt.submit":
            life.events.put_nowait(GatewayClientError("session_replay_gap"))
            return {"admission_id": "a", "status": "queued"}
        raise AssertionError(method)

    dials = []
    view = _view(refused, dials)
    with pytest.raises(GatewayClientError, match="session_replay_gap"):
        await asyncio.wait_for(view.run("q", oneshot=True), 5)
    assert view.redials == 0
