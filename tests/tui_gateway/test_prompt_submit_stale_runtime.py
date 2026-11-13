"""The sidecar's ``prompt.submit`` answers for a runtime id a backend roll dropped: the contract Desktop's
retry depends on (apps/desktop .../use-prompt-actions/submit.ts ``isStaleRuntimeRefusal``).

The hub's Desktop backend (``hermes serve``) rolls on every instant core update and forgets every
in-memory runtime. The first send after a roll names the old runtime id. Desktop sends it without
``submission_id`` (this contract refuses that key, below), so it can resend only if the refusal is a
definite "nothing was admitted". It is: the stale id is looked up before the turn claim, the user-row
write and the busy queue. These tests pin that the refusal is the bare legacy frame (code 4001, message
``session not found``, no ``data``) and that nothing was written for it.
"""

from tui_gateway import server


def test_stale_runtime_submit_is_a_bare_4001_before_anything_is_admitted(monkeypatch):
    db_calls = []
    monkeypatch.setattr(server, "_get_db", lambda: db_calls.append("db") or None)
    before = dict(server._sessions)

    resp = server.handle_request({"id": "r1", "method": "prompt.submit",
                                  "params": {"session_id": "rolled-away", "text": "typed after the roll"}})

    assert resp["error"] == {"code": 4001, "message": "session not found"}
    assert server._sessions == before
    assert db_calls == []


def test_the_sidecar_contract_refuses_submission_id_before_any_handler_runs(monkeypatch):
    """Why the hub's sends are identityless: ``PromptSubmitParams`` has no ``submission_id``."""
    ran = []
    monkeypatch.setitem(server._methods, "prompt.submit",
                        lambda rid, params: ran.append(params) or {"jsonrpc": "2.0", "id": rid, "result": {}})

    resp = server.handle_request({"id": "r2", "method": "prompt.submit",
                                  "params": {"session_id": "any", "text": "hi", "submission_id": "sub-1"}})

    assert resp["error"]["code"] == 4000
    assert "submission_id" in resp["error"]["message"]
    assert ran == []
