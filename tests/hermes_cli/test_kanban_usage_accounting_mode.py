"""Usage-accounting admission mode: observe by default, enforce opt-in with a cutoff. [daveey]

2026-11-26: the fail-closed gate held 37/37 ready default-board cards (286 runs) because
continue/block/schedule/retain/reclaim exits never write a final receipt and nothing can
settle them; the pool ran 2 cards with 28 free slots. Observe keeps the debt visible
(``pending``) without refusing; enforce with ``enforce_after`` only fences new runs.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_usage_accounting as ua
from tests.hermes_cli import test_kanban_usage_accounting as accounting

kanban_home = accounting.kanban_home


def _policy(monkeypatch, section):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"usage_accounting": section}})


def _ended(conn, tid, at):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=?", (at, tid))


def test_production_default_is_observe():
    # The autouse test fixture pins enforce; the shipped constant is observe.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ua))
    shipped = {n.targets[0].id: n.value.value for n in tree.body
               if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
               and isinstance(n.value, ast.Constant)}
    assert shipped["DEFAULT_MODE"] == "observe"


def test_observe_mode_admits_a_card_with_debt_and_keeps_it_visible(kanban_home, monkeypatch):
    _policy(monkeypatch, {"mode": "observe"})
    with kbc.connect_closing() as conn:
        tid, _ = accounting.released(conn)
        assert ua.pending(conn, tid)  # the debt is still reported
        assert ua.enforced(conn, tid) == []
        assert kb.claim_task(conn, tid) is not None
        assert sync.pop_claim_deferral(tid) is None


def test_missing_section_uses_default_mode(kanban_home, monkeypatch):
    monkeypatch.setattr(ua, "DEFAULT_MODE", "observe")
    monkeypatch.setattr("hermes_cli.config.load_config", dict)
    with kbc.connect_closing() as conn:
        tid, _ = accounting.released(conn)
        assert kb.claim_task(conn, tid) is not None


def test_enforce_mode_still_refuses(kanban_home, monkeypatch):
    _policy(monkeypatch, {"mode": "enforce"})
    with kbc.connect_closing() as conn:
        tid, _ = accounting.released(conn)
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == "usage_accounting_pending"


def test_enforce_after_fences_only_newer_runs(kanban_home, monkeypatch):
    cutoff = time.time()
    _policy(monkeypatch, {"mode": "enforce", "enforce_after": cutoff})
    with kbc.connect_closing() as conn:
        old, _ = accounting.released(conn)
        _ended(conn, old, int(cutoff) - 3600)
        new, _ = accounting.released(conn)
        _ended(conn, new, int(cutoff) + 60)
        assert kb.claim_task(conn, old) is not None
        assert kb.claim_task(conn, new) is None
        assert sync.pop_claim_deferral(new) == "usage_accounting_pending"


@pytest.mark.parametrize("section", [{"mode": "bogus"}, {"mode": "enforce", "enforce_after": "soon"}])
def test_malformed_policy_fails_closed(kanban_home, monkeypatch, section):
    _policy(monkeypatch, section)
    with kbc.connect_closing() as conn:
        tid, _ = accounting.released(conn)
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == "usage_accounting_pending"


def test_unreadable_config_fails_closed(monkeypatch):
    def boom():
        raise OSError("unreadable")
    monkeypatch.setattr("hermes_cli.config.load_config", boom)
    assert ua.gate_policy() == ("enforce", None)


def test_observe_keeps_the_spend_budget_hold(kanban_home, monkeypatch):
    _policy(monkeypatch, {"mode": "observe"})
    monkeypatch.setattr(budget, "limits", lambda: budget.Limits(runs=100, cache_read_tokens=10))
    with kbc.connect_closing() as conn:
        tid, context = accounting.released(conn)
        accounting.receipt(context)
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == "spend_budget_exhausted"
