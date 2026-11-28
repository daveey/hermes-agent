"""Agent budget renewals cannot erase the Board's still-active lease identity."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_allowance as allowance
from tests.hermes_cli import test_kanban_spend_allowance as base

kanban_home = base.kanban_home
lease_client = base.lease_client
primary = base.primary


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_agent_release_keeps_exact_reviewer_identity(primary, lease_client, lane):
    tid, uid = base._released(primary)
    base._override(primary, tid, runs=10, writer_kind="agent", by="default",
                   reason="hermes kanban release: 10 more runs (mandate comment 123)")
    if lane == "review":
        from hermes_cli import kanban_db_connect as connect
        with connect.write_txn(primary):
            primary.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    original = lease_client.acquire
    wanted = {"review_uid": base.REVIEW, "budget_event_uid": uid}

    def acquire(**kw):
        lease = original(**kw)
        if kw.get("expected_reviewer_allowance") != wanted:
            return sync.Lease(**{**lease.__dict__, "granted": False, "reason": "reviewer_allowance_changed"})
        return sync.Lease(**{**lease.__dict__, "spend_allowance": base._evidence(uid)})

    lease_client.acquire = acquire
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    result = claim(primary, tid, ttl_seconds=900, claimer="host:a")
    assert result is not None, str(sync.pop_claim_deferral(tid))
    assert allowance.state(primary, tid) == allowance.BOARD


@pytest.mark.parametrize("writer", ["human", "system", None])
def test_other_budget_writers_do_not_inherit(primary, writer):
    tid, _ = base._released(primary)
    base._override(primary, tid, runs=10, writer_kind=writer)
    assert allowance.expected(primary, tid) is None


def test_agent_cannot_skip_malformed_basis_or_human_budget(primary):
    tid, _ = base._released(primary)
    base._override(primary, tid, runs=10, runs_basis="unknown", writer_kind="agent")
    base._override(primary, tid, runs=10, writer_kind="agent")
    assert allowance.state(primary, tid) == allowance.UNPROVEN
    base._override(primary, tid, runs=10, writer_kind="human")
    base._override(primary, tid, runs=10, writer_kind="agent")
    assert allowance.expected(primary, tid) is None


@pytest.mark.parametrize("count", [1, 3])
def test_agent_chain_uses_original_uid_and_not_replica_run_count(primary, count):
    tid, uid = base._released(primary)
    for _ in range(count):
        base._override(primary, tid, runs=10, writer_kind="agent")
    base._history(primary, tid, 25)
    from hermes_cli import kanban_spend_budget as budget
    assert allowance.expected(primary, tid)["budget_event_uid"] == uid
    assert budget.over(budget.spend(primary, tid, as_of=base.LATER), base.TEN) is None


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_agent_chain_never_authorizes_nonprimary(primary, mode):
    tid, _ = base._released(primary)
    base._override(primary, tid, runs=10, writer_kind="agent")
    sync.set_mode(primary, mode)
    assert allowance.expected(primary, tid) is None
    assert allowance.state(primary, tid) == allowance.UNPROVEN


@pytest.mark.parametrize("evidence", [None, "wrong-review", "wrong-budget", "uncharged"])
def test_inherited_identity_still_requires_exact_charge(primary, lease_client, evidence):
    tid, uid = base._released(primary)
    base._override(primary, tid, runs=10, writer_kind="agent")
    bad = {None: None, "wrong-review": base._evidence(uid, review_uid="wrong"),
           "wrong-budget": base._evidence("wrong"), "uncharged": base._evidence(uid, charged=False)}
    base._grant_with(lease_client, bad[evidence])
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:a") is None
    assert str(sync.pop_claim_deferral(tid)) == allowance.REFUSAL


def test_agent_without_preceding_reviewer_is_ordinary(primary):
    tid = kb.create_task(primary, title="ordinary", assignee="worker")
    base._override(primary, tid, runs=10, writer_kind="agent")
    assert allowance.expected(primary, tid) is None
    assert allowance.state(primary, tid) is None


@pytest.mark.parametrize("raw", ["not-json", "null", "[]"])
def test_malformed_historical_row_is_a_barrier(raw):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,kind TEXT,payload TEXT)")
        conn.execute("INSERT INTO task_events VALUES(1,'old','t','spend_budget_set',?)", (raw,))
        data = {"runs": 10, "writer_kind": "agent"}
        assert allowance.expected(conn, "t", data) is None
    finally:
        conn.close()

