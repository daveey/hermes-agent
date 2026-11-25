"""Spend admission keeps the existing accounting and publication-owner contracts. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_claim as admission
from hermes_cli import kanban_spend_pr_owner as owner
from tests.hermes_cli import test_kanban_spend_pr_owner as helpers

kanban_home = helpers.kanban_home
github = helpers.github
_REAL_LIMITS = budget.limits


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("open_pr", [False, True])
def test_claim_keeps_publication_owner_exception(kanban_home, github, lane, open_pr):
    github[0]["state"] = "OPEN" if open_pr else "CLOSED"
    with kbc.connect_closing() as conn:
        tid = helpers.owner(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert (claim(conn, tid) is not None) == open_pr
        assert budget.spend(conn, tid).runs == budget.DEFAULT_RUNS


def test_publication_observation_never_runs_under_claim_write_lock(kanban_home, github, monkeypatch):
    real = owner.open_core_pr
    calls = []

    def observed(conn, tid):
        assert not conn.in_transaction
        calls.append(tid)
        return real(conn, tid)

    monkeypatch.setattr(owner, "open_core_pr", observed)
    with kbc.connect_closing() as conn:
        tid = helpers.owner(conn)
        assert kb.claim_task(conn, tid) is not None
        assert calls == [tid]


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_config_outage_refuses_claim_without_escaping(kanban_home, monkeypatch, lane):
    from hermes_cli.config_backend import ConfigBackendUnavailable

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="config outage claim", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))

        def down():
            raise ConfigBackendUnavailable("down")

        monkeypatch.setattr(budget, "limits", _REAL_LIMITS)
        monkeypatch.setattr("hermes_cli.config.load_config", down)
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert claim(conn, tid) is None
        from hermes_cli import kanban_db_sync as sync
        refusal = sync.pop_claim_deferral(tid)
        assert refusal == "spend_policy_unavailable"
        assert refusal.origin == sync.ORIGIN_LOCAL
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == lane
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("pending", [False, True])
def test_foreign_spend_refusal_is_local_before_board_lease(kanban_home, github, monkeypatch, lane, pending):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_dispatch_verdict import claim_refused
    from types import SimpleNamespace

    with kbc.connect_closing() as conn:
        tid = helpers.owner(conn)
        sync.apply_sync_schema_steps(conn)
        github[0]["state"] = "CLOSED"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?, created_on='dead-worker' WHERE id=?", (lane, tid))
        if pending:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "spend_budget_triaged", {"cause": "capability_unavailable"})
                kb._append_event(conn, tid, "triage_swept", {"assignee": "worker"})
        assert decision.pending(conn, tid) == pending

        def unexpected_lease(*args, **kwargs):
            pytest.fail("a retained/exhausted task must not request a Board lease")

        monkeypatch.setattr(sync, "claim_gate", unexpected_lease)
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        before = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        assert claim(conn, tid) is None
        refusal = sync.pop_claim_deferral(tid)
        assert refusal is not None
        assert refusal == ("decision_pending" if pending else "spend_budget_exhausted")
        assert refusal.origin == sync.ORIGIN_LOCAL
        result = SimpleNamespace(claim_deferred=[], quiet_deferred=[], capsule_refused=[])
        claim_refused(result, tid, refusal)
        assert result.claim_deferred == [(tid, refusal)]
        from hermes_cli.kanban_db_dispatch_stuck import stuck_warning
        warning = stuck_warning(6, [kb.DispatchResult(claim_deferred=[(tid, refusal)])])
        assert "before the Board lease" in warning
        assert "Board lease refused" not in warning
        assert "owning chat" in warning
        assert sync.pop_claim_deferral(tid) is None
        assert kb.get_task(conn, tid).status == lane
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == before


def test_transaction_rechecks_override_and_publication_identity(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = helpers.owner(conn)
        check = admission.claim_check(conn, tid)
        with kb.write_txn(conn):
            assert not check()
            conn.execute("UPDATE task_runs SET metadata=NULL WHERE task_id=?", (tid,))
            assert check()  # a preflight OPEN result is not a blanket spend bypass
        budget.set_override(conn, tid, runs=0, cache_read_tokens=0, by="fixture", reason="disabled control")
        with kb.write_txn(conn):
            assert not check()


def test_new_exhaustion_after_preflight_waits_for_next_pr_observation(kanban_home, github):
    from tests.hermes_cli import test_kanban_spend_budget as spending

    with kbc.connect_closing() as conn:
        tid = helpers.owner(conn, runs=budget.DEFAULT_RUNS - 1)
        check = admission.claim_check(conn, tid)
        assert github[1] == []
        spending._run(conn, tid)
        with kb.write_txn(conn):
            assert check()
        assert not admission.claim_check(conn, tid)()
