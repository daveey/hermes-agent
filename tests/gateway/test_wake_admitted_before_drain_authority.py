"""A wake receipted before a restart drain must run exactly once on the authority route too.

Once a chat has a committed human turn, ``deliver_wake`` commits a Kanban wake to the session
authority's FIFO (``admit_route_wake``) instead of the classic adapter path the sibling file covers.
The receipt lets the notifier advance its cursor, so the wake is never re-sent: a drain must leave
the committed row queued (or run it), and the restarted gateway's startup recovery must run it, never
twice. Real GatewayRunner, DiscordAdapter and SessionAuthority over SQLite; the restart is a second
runner and authority over the same state DB, started only once the first runner's restart drain
(``_drain_active_agents``) has joined its in-flight work, as a real restart does: a claim the owner
committed before the drain began settles under the owner's epoch, never after the successor's.
"""

import asyncio
import threading

import pytest
pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")

from tests.waiting import async_wait_until

WAKE = "[Kanban task completed] t_1"


class Hold:
    """Keeps the human turn running until ``finish`` is set."""

    def __init__(self):
        self.running, self.finish = asyncio.Event(), asyncio.Event()


async def authority_route(monkeypatch, instance_id, executed, hold=None):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))

    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="sent")

    monkeypatch.setattr(adapter, "send", send)
    adapter.gateway_runner = runner  # as GatewayRunner._create_adapter binds every adapter
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id=instance_id)
    runner._wire_adapter_handlers(adapter)

    async def turn(event, source, quick_key, run_generation):
        executed.append((instance_id, event.text))
        if hold is not None and event.text == "A":
            hold.running.set()
            await hold.finish.wait()
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": "reply " + event.text, "messages": []}
        return "reply " + event.text

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    return runner, adapter, authority, adapter.build_source(chat_id="42", chat_type="dm", user_id="42")


def admissions(runner, authority, source):
    from hermes_state_runtime import list_session_admissions

    owner = runner.session_store.peek_session_id(runner.session_store._generate_session_key(source))
    rows = list_session_admissions(authority.db, session_id=owner, pending_only=False)
    return [(row["principal_id"].split(":")[0], row["status"], row.get("outcome")) for row in rows]


def wake_runs(executed):
    return [owner for owner, text in executed if text == WAKE]


def hold_owner_wake_claim(monkeypatch):
    """Parks the owner's claim of the wake inside its write transaction, after the admission gate
    passed: the claim is in flight (not yet committed or visible) when the restart drain begins.
    Returns ``(gate_passed, release)`` threading events."""
    from gateway import session_authority
    from hermes_state_runtime import get_session_admission

    claim, gate_passed, release = session_authority.claim_session_input, threading.Event(), threading.Event()

    def held_claim(db, **kwargs):
        guard, head = kwargs.get("_guard"), kwargs.get("head")

        def gate(conn):
            if guard is not None:
                guard(conn)
            row = get_session_admission(db, admission_id=head) if head else None
            if row and row["principal_id"].startswith("automation") and not gate_passed.is_set():
                gate_passed.set()
                assert release.wait(30), "the test never released the held wake claim"

        return claim(db, **{**kwargs, "_guard": gate})

    monkeypatch.setattr(session_authority, "claim_session_input", held_claim)
    return gate_passed, release


@pytest.mark.asyncio
@pytest.mark.parametrize("busy,claim_in_flight", [(False, False), (True, False), (False, True)],
                         ids=["idle", "busy", "claim_in_flight"])
async def test_authority_route_runs_a_wake_receipted_before_drain_once_after_restart(
        monkeypatch, busy, claim_in_flight):
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import recover_profile_native_sessions, settle_gateway_runtime
    from gateway.wake import deliver_wake

    held = hold_owner_wake_claim(monkeypatch) if claim_in_flight else None
    executed, hold = [], Hold() if busy else None
    runner, adapter, authority, source = await authority_route(monkeypatch, "owner", executed, hold)
    await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
    if hold is not None:
        await asyncio.wait_for(hold.running.wait(), 10)
    else:
        await async_wait_until(
            lambda: admissions(runner, authority, source) == [("messaging", "terminal", "completed")],
            what="the human turn to settle",
        )
    await asyncio.wait_for(deliver_wake(adapter, text=WAKE, source=source), 10)  # receipt: no retry
    if held is not None:  # a starved loop: the owner's drain reached the wake's claim first
        await async_wait_until(held[0].is_set, what="the owner's claim of the wake")
    runner._draining = True  # /restart begins before the wake's turn is claimed (or committed)
    if held is not None:
        held[1].set()
    if hold is not None:
        hold.finish.set()
    _snapshot, timed_out = await runner._drain_active_agents(timeout=30)  # the restart's drain step
    assert not timed_out
    await async_wait_until(
        lambda: admissions(runner, authority, source)[0] == ("messaging", "terminal", "completed"),
        what="the human turn to settle",
    )
    # Join authority tasks before the successor takes the profile.
    await settle_gateway_runtime(runner)
    wake_row = admissions(runner, authority, source)[-1]
    assert wake_row[0] == "automation"
    assert wake_row[1] == "queued" or (wake_row[1:] == ("terminal", "completed") and wake_runs(executed) == ["owner"])

    successor, _adapter, successor_authority, _source = await authority_route(monkeypatch, "successor", executed)
    await recover_profile_native_sessions(successor, successor_authority)  # a real gateway's startup step
    await async_wait_until(
        lambda: admissions(successor, successor_authority, source)[-1] == ("automation", "terminal", "completed"),
        what="the wake to run",
    )
    assert len(wake_runs(executed)) == 1  # ran once, on whichever gateway claimed it


@pytest.mark.asyncio
async def test_a_wake_claimed_just_before_a_drain_runs_rather_than_settling_unrun(monkeypatch):
    """The drain begins after the wake's claim committed (its admission gate already passed): the
    old owner holds the only claim, so it must run the wake, not answer it with the drain notice."""
    from gateway.platforms.event import MessageEvent
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.wake import deliver_wake

    executed = []
    runner, adapter, authority, source = await authority_route(monkeypatch, "owner", executed)
    await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
    await async_wait_until(
        lambda: admissions(runner, authority, source) == [("messaging", "terminal", "completed")],
        what="the human turn to settle",
    )
    stamp = authority._stamp_claim

    def drain_begins_after_commit(ref, live, row, **kwargs):
        if row:
            runner._draining = True
        return stamp(ref, live, row, **kwargs)

    monkeypatch.setattr(authority, "_stamp_claim", drain_begins_after_commit)
    await asyncio.wait_for(deliver_wake(adapter, text=WAKE, source=source), 10)
    await settle_gateway_runtime(runner)
    assert admissions(runner, authority, source)[-1] == ("automation", "terminal", "completed")
    assert wake_runs(executed) == ["owner"]
