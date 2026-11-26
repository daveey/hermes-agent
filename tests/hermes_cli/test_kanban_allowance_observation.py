"""Allowance diagnostics are bounded authenticated reads, never lease authority."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_allowance_observation as observation
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_spend_allowance as fixtures

kanban_home = fixtures.kanban_home
primary = fixtures.primary


def _reply(conn, tid, uid, **changes):
    return {"board": sync.sync_state(conn)["board_id"], "allowance": {
        "runs_basis": "board_lease_allowance_v1", "task": tid, "review_uid": fixtures.REVIEW,
        "budget_event_uid": uid, "episode": "stop-1", "home": "test-home", "state": "active",
        "runs": 10, "used": 10, "remaining": 0, **changes}}


def _hub(monkeypatch, conn, reply):
    calls = []
    class Hub:
        def __init__(self, ident, *, deadline):
            assert not conn.in_transaction
            calls.append(("open", deadline))
        def reviewer_allowance(self, board, task):
            assert not conn.in_transaction
            calls.append(("read", board, task))
            if isinstance(reply, Exception):
                raise reply
            return reply
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test-home"))
    monkeypatch.setattr(client, "Hub", Hub)
    return calls


def test_exhaustion_is_exact_diagnostic_not_claim_evidence(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    calls = _hub(monkeypatch, primary, _reply(primary, tid, uid))
    result = observation.read(primary, tid)
    assert result is not None and result.remaining == 0
    assert result.expected == fixtures.allowance.expected(primary, tid)
    assert result.board == sync.sync_state(primary)["board_id"]
    assert observation.current(primary, tid, result)
    assert calls[1] == ("read", result.board, tid)
    assert calls[-1] == ("close",)
    assert not fixtures.allowance.evidence_ok(result.expected, result)


@pytest.mark.parametrize("changes", [
    {"task": "other"}, {"review_uid": "other"}, {"budget_event_uid": "other"},
    {"runs_basis": "unknown"}, {"state": "invalidated"}, {"runs": 11}, {"runs": True},
    {"used": True}, {"used": -1}, {"used": 11}, {"remaining": True},
    {"remaining": -1}, {"remaining": 11}, {"remaining": 1}, {"home": ""}, {"episode": ""},
])
def test_malformed_or_wrong_coordinates_never_prove_exhaustion(primary, monkeypatch, changes):
    tid, uid = fixtures._released(primary)
    _hub(monkeypatch, primary, _reply(primary, tid, uid, **changes))
    assert observation.read(primary, tid) is None


@pytest.mark.parametrize("reply", [None, [], {}, {"board": "wrong", "allowance": {}},
                                  client.HubError(503, "rollover"), TimeoutError("deadline")])
def test_absent_or_failed_read_does_not_invent_exhaustion(primary, monkeypatch, reply):
    tid, _ = fixtures._released(primary)
    calls = _hub(monkeypatch, primary, reply)
    assert observation.read(primary, tid) is None
    assert calls[-1] == ("close",)


def test_write_transaction_refuses_before_identity_or_network(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    calls = _hub(monkeypatch, primary, _reply(primary, tid, uid))
    with fixtures.kbc.write_txn(primary):
        assert observation.read(primary, tid) is None
    assert calls == []


def test_successor_override_invalidates_observation(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    _hub(monkeypatch, primary, _reply(primary, tid, uid))
    result = observation.read(primary, tid)
    fixtures._override(primary, tid, runs=10, runs_basis=fixtures.allowance.BASIS, review_uid="successor")
    assert not observation.current(primary, tid, result)


def test_observation_deadline_and_expiry(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    clock = [100.0]
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])
    calls = _hub(monkeypatch, primary, _reply(primary, tid, uid))
    result = observation.read(primary, tid)
    assert calls[0] == ("open", 105.0)
    clock[0] = 106.0
    assert not observation.current(primary, tid, result)


def test_route_quotes_coordinates_and_bounds_response():
    calls = []
    class Hub(client.HubRoutesMixin):
        def _call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}
    assert Hub().reviewer_allowance("a/b", "task&other") == {"ok": True}
    assert calls == [(("GET", "/v2/boards/a%2Fb/reviewer-allowance?task=task%26other"),
                      {"max_response_bytes": 8192})]


def test_late_reply_is_not_an_observation(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    from itertools import chain, repeat
    times = chain([100.0], repeat(106.0))
    monkeypatch.setattr(observation.time, "monotonic", lambda: next(times))
    _hub(monkeypatch, primary, _reply(primary, tid, uid))
    assert observation.read(primary, tid) is None


def test_expired_shared_deadline_does_not_open_transport(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    monkeypatch.setattr(observation.time, "monotonic", lambda: 100.0)
    calls = _hub(monkeypatch, primary, _reply(primary, tid, uid))
    assert observation.read(primary, tid, deadline=100.0) is None
    assert calls == []


def test_shared_deadline_cannot_extend_per_read_bound(primary, monkeypatch):
    tid, uid = fixtures._released(primary)
    monkeypatch.setattr(observation.time, "monotonic", lambda: 100.0)
    calls = _hub(monkeypatch, primary, _reply(primary, tid, uid))
    assert observation.read(primary, tid, deadline=1000.0) is not None
    assert calls[0] == ("open", 105.0)
