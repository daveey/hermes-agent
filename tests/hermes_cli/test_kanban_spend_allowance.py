"""Portable board_lease_allowance_v1 consumer: the Board's lease grant counts, nothing local ever does."""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_allowance as allowance
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import kanban_sync_support as support

TEN = budget.Limits(10, 0)
REVIEW = "rev-0001"
LATER = int(time.time()) + 100_000  # read spend as of after the synthetic history below


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def lease_client():
    yield from support.lease_client()


@pytest.fixture
def primary(kanban_home):
    for conn in support.synced_conn(kanban_home):
        sync.set_mode(conn, "primary")
        yield conn


def _override(conn, tid, **payload):
    with kbc.write_txn(conn):
        kb._append_event(conn, tid, budget.OVERRIDE_EVENT, payload)
    if "uid" not in {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}:
        return None
    return conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                        (tid, budget.OVERRIDE_EVENT)).fetchone()[0]


def _released(conn, **extra):
    tid = kb.create_task(conn, title="reviewed", assignee="worker")
    uid = _override(conn, tid, **{"runs": 10, "cache_read_tokens": None, "runs_basis": allowance.BASIS,
                                  "review_uid": REVIEW, "by": "kanban-spend-reviewer", **extra})
    return tid, uid


def _history(conn, tid, n):
    now = int(time.time()) + 10
    with kbc.write_txn(conn):
        for i in range(n):
            conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                         "VALUES (?, 'crashed', 'crashed', ?, ?)", (tid, now + 200 * i, now + 200 * i + 120))


def _evidence(uid, **over):
    return {"basis": allowance.BASIS, "review_uid": REVIEW, "budget_event_uid": uid, "charged": True,
            "remaining": 9, **over}


def _grant_with(lease_client, evidence):
    original = lease_client.acquire

    def acquire(**kw):
        lease = original(**kw)
        return sync.Lease(**{**lease.__dict__, "spend_allowance": evidence})
    lease_client.acquire = acquire


@pytest.mark.parametrize("noise", ["foreign_budget", "other_kind"])
def test_expected_ignores_newer_unrelated_events(primary, noise):
    tid, event_uid = _released(primary)
    if noise == "foreign_budget":
        _released(primary)
    else:
        with kbc.write_txn(primary):
            kb._append_event(primary, tid, "fixture_other", {"runs_basis": allowance.BASIS})
    expected = allowance.expected(primary, tid)
    assert expected is not None and expected["budget_event_uid"] == event_uid


@pytest.mark.parametrize("data", [False, [], "malformed"])
def test_explicit_nonmapping_override_is_legacy_not_an_exception(primary, data):
    tid, _ = _released(primary)
    assert allowance.expected(primary, tid, data) is None
    assert allowance.state(primary, tid, data) is None


def test_explicit_valid_data_without_event_has_no_authority(primary):
    tid = kb.create_task(primary, title="no override", assignee="worker")
    assert allowance.expected(primary, tid, {"runs_basis": allowance.BASIS, "runs": 10, "review_uid": REVIEW}) is None


def test_legacy_schema_without_uid_is_unproven_not_an_exception():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)")
        data = {"runs_basis": allowance.BASIS, "runs": 10, "review_uid": REVIEW}
        conn.execute("INSERT INTO task_events(task_id,kind,payload) VALUES (?,?,?)", ("t", budget.OVERRIDE_EVENT, json.dumps(data)))
        assert allowance.expected(conn, "t", data) is None
        assert allowance.state(conn, "t", data) == allowance.UNPROVEN
    finally:
        conn.close()


def test_replica_history_never_counts_for_a_recognized_board_allowance(primary):
    tid, _ = _released(primary)
    _history(primary, tid, 25)  # replica history far past the fixed ten
    spent = budget.spend(primary, tid, as_of=LATER)
    assert spent.allowance == allowance.BOARD and spent.runs >= 10
    assert budget.over(spent, TEN) is None


def test_cache_limit_still_cumulative_under_board_allowance(primary):
    tid, _ = _released(primary)
    spent = budget.spend(primary, tid)._replace(tokens={**budget.spend(primary, tid).tokens, "cache_read_tokens": 5})
    assert budget.over(spent, budget.Limits(10, 5)) == "cache_read_tokens"


@pytest.mark.parametrize("extra", [{"runs": 10.0}, {"runs": True}, {"runs": 11}, {"review_uid": ""},
                                   {"review_uid": 123}, {"runs_basis": None}, {"runs_basis": ""},
                                   {"runs_basis": "source_attempt_allowance_v1"}, {"runs_basis": "v2"}])
def test_malformed_or_unknown_basis_holds_without_cumulative_fallback(primary, extra):
    tid, _ = _released(primary, **extra)  # zero history: a cumulative fallback would admit
    spent = budget.spend(primary, tid)
    assert spent.allowance == allowance.UNPROVEN
    assert budget.over(spent, TEN) == "runs"


def test_unsynced_board_never_grants_a_local_allowance(kanban_home):
    conn = kbc.connect(kanban_home / "kanban.db")
    try:
        tid, _ = _released(conn)
        assert budget.over(budget.spend(conn, tid), TEN) == "runs"
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_nonprimary_mode_with_valid_uid_never_authorizes(primary, mode):
    tid, uid = _released(primary)
    assert uid and allowance.expected(primary, tid) is not None
    sync.set_mode(primary, mode)
    assert allowance.expected(primary, tid) is None
    assert allowance.state(primary, tid) == allowance.UNPROVEN
    assert budget.over(budget.spend(primary, tid), TEN) == "runs"


def test_claim_admits_only_with_exact_board_charge(primary, lease_client):
    tid, uid = _released(primary)
    _history(primary, tid, 25)
    _grant_with(lease_client, _evidence(uid))
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:a") is not None


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("evidence", [None, "missing-field", "wrong-review", "wrong-budget", "uncharged",
                                      "float-remaining", "negative", "basis", "full", "bool", "truthy"])
def test_old_or_skewed_board_evidence_refuses_and_releases(primary, lease_client, evidence, lane):
    tid, uid = _released(primary)
    if lane == "review":
        with kbc.write_txn(primary):
            primary.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    bad = {None: None, "missing-field": {k: v for k, v in _evidence(uid).items() if k != "remaining"},
           "wrong-review": _evidence(uid, review_uid="rev-other"), "wrong-budget": _evidence(uid, budget_event_uid="x"),
           "uncharged": _evidence(uid, charged=False), "float-remaining": _evidence(uid, remaining=9.0),
           "full": _evidence(uid, remaining=10), "bool": _evidence(uid, remaining=True),
           "truthy": _evidence(uid, charged=1),
           "negative": _evidence(uid, remaining=-1), "basis": _evidence(uid, basis="source_attempt_allowance_v1")}
    _grant_with(lease_client, bad[evidence])
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(primary, tid, ttl_seconds=900, claimer="host:a") is None
    assert str(sync.pop_claim_deferral(tid)) == allowance.REFUSAL
    assert primary.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == lane
    assert [kw.get("unused_grant") for kind, kw in lease_client.calls if kind == "release"] == [True]


def test_newer_ordinary_override_supersedes_allowance(primary, lease_client):
    tid, _uid = _released(primary)
    _override(primary, tid, runs=10, cache_read_tokens=None, by="someone")
    _history(primary, tid, 12)
    spent = budget.spend(primary, tid, as_of=LATER)
    assert spent.allowance is None and spent.runs >= 10
    assert budget.over(spent, TEN) == "runs"  # cumulative again


def test_hub_client_parses_grant_evidence_only_when_granted():
    from hermes_cli import kanban_sync_client as ksc

    class FakeHub:
        def __init__(self, res):
            self.res = res

        def leases(self, board, body):
            return self.res
    ev = {"basis": allowance.BASIS, "remaining": 3}
    granted = ksc.HubLeaseClient(FakeHub({"status": "applied", "lease": {"run_uid": "r1", "expires": 1, "task_id": "t", "holder": "g"},
                                          "spend_allowance": ev}))
    assert granted.acquire(board_id="b", task_uid="t", run_uid="r1", gateway_id="g", ttl_s=5).spend_allowance == ev
    denied = ksc.HubLeaseClient(FakeHub({"status": "rejected", "reason": "spend_allowance_exhausted",
                                         "spend_allowance": ev}))
    lease = denied.acquire(board_id="b", task_uid="t", run_uid="r1", gateway_id="g", ttl_s=5)
    assert not lease.granted and lease.spend_allowance is None and lease.reason == "spend_allowance_exhausted"
    assert json.dumps(ev)


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_expected_identity_reaches_claim_client(primary, lease_client, lane):
    tid, event = _released(primary)
    if lane == "review":
        with kbc.write_txn(primary):
            primary.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    _grant_with(lease_client, _evidence(event))
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(primary, tid, ttl_seconds=900, claimer="host:a") is not None
    acquire = [kw for kind, kw in lease_client.calls if kind == "acquire"]
    assert acquire[0]["expected_reviewer_allowance"] == {"review_uid": REVIEW, "budget_event_uid": event}


def test_portable_reply_cannot_authorize_legacy_local_view(primary, lease_client):
    tid = kb.create_task(primary, title="status page before budget page", assignee="worker")
    _grant_with(lease_client, _evidence("missing-budget-page"))
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:a") is None
    assert [kw.get("unused_grant") for kind, kw in lease_client.calls if kind == "release"] == [True]


@pytest.mark.parametrize("remaining", [0, 9])
def test_last_and_first_charged_grants_admit(primary, lease_client, remaining):
    tid, event = _released(primary)
    _grant_with(lease_client, _evidence(event, remaining=remaining))
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:a") is not None


def test_expected_uses_newest_budget_identity(primary):
    tid, old = _released(primary)
    new = _override(primary, tid, runs=10, runs_basis=allowance.BASIS, review_uid="successor")
    assert new != old
    assert allowance.expected(primary, tid) == {
        "basis": allowance.BASIS, "review_uid": "successor", "budget_event_uid": new}


@pytest.mark.parametrize("field", ["run_uid", "task_id", "holder"])
def test_hub_client_wrong_lease_identity_never_supplies_charge(field):
    from hermes_cli import kanban_sync_client as ksc

    class Hub:
        def leases(self, board, body):
            return {"status": "applied", "lease": {"run_uid": "r", "task_id": "t", "holder": "g", field: "foreign"},
                    "spend_allowance": _evidence("e")}

    lease = ksc.HubLeaseClient(Hub()).acquire(board_id="b", task_uid="t", run_uid="r", gateway_id="g", ttl_s=5)
    assert lease.spend_allowance is None


def test_terminal_reservation_never_invents_local_allowance(primary, monkeypatch):
    from hermes_cli import kanban_terminal_run as terminal_run, kanban_continuation as continuation
    tid, _ = _released(primary)
    monkeypatch.setattr(continuation, "park_state", lambda *_: {
        "timed": True, "payload": {"wake_check": "true", "probe_host": "remote-fixture"}})
    observed = budget.spend(primary, tid)._replace(runs=9)
    monkeypatch.setattr(budget, "spend", lambda *_: observed)
    assert not terminal_run.reserve(primary, tid, "continuation_woken")
    assert primary.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='terminal_run_reserved'", (tid,)).fetchone()[0] == 0
    # Adjacent ordinary-accounting control proves the tested guard is reached.
    observed = observed._replace(allowance=None)
    with kbc.write_txn(primary):
        assert terminal_run.reserve(primary, tid, "continuation_woken")


def test_final_boundary_rejects_unproven_even_if_earlier_gate_was_bypassed(primary):
    from types import SimpleNamespace
    tid, event = _released(primary, runs_basis="retired")
    assert allowance.grant_refusal(primary, tid, SimpleNamespace(lease=SimpleNamespace(spend_allowance=_evidence(event)))) == allowance.REFUSAL


def test_missing_budget_uid_never_authorizes(primary):
    tid, _ = _released(primary)
    with kbc.write_txn(primary):
        primary.execute("UPDATE task_events SET uid=NULL WHERE task_id=? AND kind=?", (tid, budget.OVERRIDE_EVENT))
    assert allowance.state(primary, tid) == allowance.UNPROVEN


@pytest.mark.parametrize("got", [None, [], "bad", True])
def test_malformed_proof_is_not_evidence(got):
    assert not allowance.evidence_ok({"basis": allowance.BASIS}, got)
    assert not allowance.evidence_ok(None, _evidence("e"))


def test_claim_carries_exact_budget_identity(primary, lease_client):
    tid, event_uid = _released(primary)
    original = lease_client.acquire
    calls = []

    def acquire(**kwargs):
        calls.append(kwargs)
        lease = original(**kwargs)
        return sync.Lease(**{**lease.__dict__, "spend_allowance": _evidence(event_uid)})

    lease_client.acquire = acquire
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:a") is not None
    assert calls[0]["expected_reviewer_allowance"] == {"review_uid": REVIEW, "budget_event_uid": event_uid}


@pytest.mark.parametrize("status,proof", [("rejected", _evidence("e")), ("applied", []), ("applied", "bad")])
def test_hub_client_never_attaches_ungranted_or_nonmapping_proof(status, proof):
    from hermes_cli import kanban_sync_client as ksc

    class Hub:
        def leases(self, board, body):
            return {"status": status, "reason": "fixture", "lease": {
                "task_id": "t", "run_uid": "r", "holder": "g"}, "spend_allowance": proof}

    result = ksc.HubLeaseClient(Hub()).acquire(board_id="b", task_uid="t", run_uid="r", gateway_id="g", ttl_s=5)
    assert result.spend_allowance is None


def test_hub_client_always_sends_observed_allowance_identity():
    from hermes_cli import kanban_sync_client as ksc
    calls = []

    class Hub:
        def leases(self, board, body):
            calls.append(body)
            return {"status": "rejected", "reason": "fixture"}

    lease = ksc.HubLeaseClient(Hub())
    args = dict(board_id="b", task_uid="t", run_uid="r", gateway_id="g", ttl_s=5)
    lease.acquire(**args)
    assert "expected_reviewer_allowance" in calls[-1] and calls[-1]["expected_reviewer_allowance"] is None
    identity = {"review_uid": "r1", "budget_event_uid": "e1"}
    lease.acquire(**args, expected_reviewer_allowance=identity)
    assert calls[-1]["expected_reviewer_allowance"] == identity
