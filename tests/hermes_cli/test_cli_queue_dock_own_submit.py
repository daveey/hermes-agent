"""The classic CLI's Queue dock never lists the typed turn it is submitting or running.

The owner commits a plain (non-``/queue``) ``prompt.submit`` and publishes its pending list, which
lists that admission ``queued`` by ``input_id`` (= the request's ``submission_id``), before the receipt
returns and before the drain's claim publishes a list without it. The dock painted the user's own prompt
as queued in that window, and the Linux PTY e2e caught the prompt twice on screen. Real
``GatewayConnection`` / ``GatewaySessionStore`` / ``GatewayAgent`` / ``queued_prompts``; the owner is
the shared ``FakeClient`` with its publishes scripted onto the event stream.
"""
from hermes_cli.cli_session_dock import queued_prompts
from hermes_cli.gateway_agent import GatewayAgent
from hermes_cli.gateway_client import GatewayRPCError
from tests.hermes_cli.test_gateway_agent import SID, FakeClient, frame
from tests.hermes_cli.test_gateway_tools_notify import _idle_cli
from tests.waiting import async_wait_until

OWN, OTHER = "typed while idle omega-q9", "another viewer's queued prompt"


def _row(admission, input_id, text, sequence):
    return {"admission_id": admission, "input_id": input_id, "status": "queued", "sequence": sequence,
            "text": text}


def _info(*rows):
    return frame("session.info", {"pending": list(rows)}, admission="")


class _Owner(FakeClient):
    """At ``prompt.submit`` publishes the admission's pending list (own row ``queued``, plus another
    viewer's queued row) before answering, as ``SessionAuthority._admitted`` does; records what the dock
    painted then. ``refuse`` answers the submit with that refusal instead (nothing admitted)."""

    def __init__(self, script, cli, refuse=None, **kw):
        super().__init__(script, **kw)
        self.cli, self.refuse, self.dock_at_submit = cli, refuse, None

    async def rpc(self, method, *, _timeout=None, **params):
        if method != "prompt.submit":
            return await super().rpc(method, _timeout=_timeout, **params)
        self.calls.append((method, params))
        if self.refuse:
            raise GatewayRPCError(self.refuse)
        await self.events.put(_info(_row("adm-o", "other-viewer", OTHER, 1),
                                    _row("adm-1", params["submission_id"], OWN, 2)))
        state = self.cli._session_db.connection.state(SID)
        await async_wait_until(lambda: len(state.pending) == 2, timeout=5)
        self.dock_at_submit = queued_prompts(self.cli)
        self.calls.pop()
        return await super().rpc(method, _timeout=_timeout, **params)


def _agent_cli(owner_script, refuse=None):
    from hermes_cli.gateway_connection import GatewayConnection
    from tests.hermes_cli.test_gateway_agent import connect_with
    owner = _Owner(owner_script, object(), refuse=refuse, result={"final_response": "OMEGA settled reply",
                                                               "completed": True})
    conn = GatewayConnection(connect=connect_with(owner))
    conn.start(5)
    conn.state(SID)
    cli = _idle_cli(conn)
    owner.cli = cli
    cli.agent = GatewayAgent(connection=conn, session_id=SID)
    return conn, owner, cli, cli.agent


def _republish_own(conn, owner, submission):
    """A later list naming that request id ``queued`` again: shown only once no turn of this agent
    still owns it (the identity does not outlive its ``run_conversation``)."""
    import asyncio
    from tests.waiting import wait_until
    state = conn.state(SID)
    seq = state.pending_seq
    asyncio.run_coroutine_threadsafe(owner.events.put(_info(_row("adm-1", submission, OWN, 2))),
                                     conn.loop).result(5)
    wait_until(lambda: state.pending_seq > seq, timeout=5)
    return queued_prompts(owner.cli)


def test_own_typed_submit_is_never_painted_queued_and_other_queues_stay():
    script = [_info(_row("adm-o", "other-viewer", OTHER, 1)),  # the claim's publish: own row gone
              frame("message.start", {}), frame("message.delta", {"text": "OMEGA settled reply"}),
              frame("message.complete", {"text": "OMEGA settled reply", "status": "complete"})]
    conn, owner, cli, agent = _agent_cli(script)
    try:
        result = agent.run_conversation(OWN, conversation_history=[])
        assert result["final_response"] == "OMEGA settled reply"
        assert owner.dock_at_submit == [OTHER], "own submitting turn painted as a queued prompt"
        assert queued_prompts(cli) == [OTHER]
        submission = next(p["submission_id"] for m, p in owner.calls if m == "prompt.submit")
        assert _republish_own(conn, owner, submission) == [OWN]  # cleared after a settled turn
    finally:
        conn.close()


def test_a_refused_submit_leaves_no_own_identity_behind():
    conn, owner, _cli, agent = _agent_cli([], refuse="session_busy")
    try:
        result = agent.run_conversation(OWN, conversation_history=[])
        assert result["failure_reason"] == "session_busy"
        submission = next(p["submission_id"] for m, p in owner.calls if m == "prompt.submit")
        assert _republish_own(conn, owner, submission) == [OWN]
    finally:
        conn.close()
