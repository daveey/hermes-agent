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
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == lane
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0


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
