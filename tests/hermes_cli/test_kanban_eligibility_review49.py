"""Preclaim eligibility faults stay card-local, including review and pickup."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_dispatch import dispatch_once
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / '.hermes'
    h.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(h))
    (h / 'config.yaml').write_text(_managed_yaml())
    kb.init_db()
    return h


def _card(conn, *, assignee='default', review=False, priority=100):
    tid = kb.create_task(conn, title='Eligibility check', assignee=assignee, priority=priority)
    if review:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    return tid


def _untouched(conn, tid, status):
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == status
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert task.consecutive_failures == 0
    assert kb.list_runs(conn, tid) == []
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='claimed'", (tid,)).fetchone()


@pytest.mark.parametrize('bad', ['providers: [x]\n', 'providers:\n  anthropic: nope\n'])
@pytest.mark.parametrize('review', [False, True])
def test_nested_config_fault_isolated_and_recovers(home, bad, review):
    profile = home / 'profiles' / 'broken'
    profile.mkdir(parents=True)
    config = profile / 'config.yaml'
    config.write_text(_managed_yaml() + bad)
    with kbc.connect_closing() as conn:
        broken = _card(conn, assignee='broken', review=review)
        peer = _card(conn, priority=1)
        launched = []
        result = dispatch_once(conn, spawn_fn=lambda task, *a, **k: launched.append(task.id))
        assert peer in launched
        assert broken not in launched
        assert (broken, 'model_unroutable:config_unavailable') in result.skipped_ineligible
        _untouched(conn, broken, 'review' if review else 'ready')
        config.write_text(_managed_yaml())
        dispatch_once(conn, spawn_fn=lambda task, *a, **k: launched.append(task.id))
        assert broken in launched
        assert len(kb.list_runs(conn, broken)) == 1


@pytest.mark.parametrize('policy', ['model_routing:\n  enabled: false\n', 'terminal:\n  backend: local\n'])
@pytest.mark.parametrize('review_kind', ['lane', 'skill', 'downstream'])
def test_routing_off_review_never_claimed_and_recovers(home, monkeypatch, policy, review_kind):
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    profile = home / 'profiles' / 'off'
    profile.mkdir(parents=True)
    config = profile / 'config.yaml'
    config.write_text(policy)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Independent exact-head review' if review_kind == 'downstream' else 'Head check',
                             assignee='off', skills=['sdlc-review'] if review_kind == 'skill' else [], priority=100)
        if review_kind == 'lane':
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        peer = _card(conn, priority=1)
        launched = []
        for _ in range(4):
            result = dispatch_once(conn, spawn_fn=lambda task, *a, **k: launched.append(task.id))
            assert (tid, 'model_unroutable:routing_disabled') in result.skipped_ineligible
            _untouched(conn, tid, 'review' if review_kind == 'lane' else 'ready')
        assert peer in launched
        assert tid not in launched
        config.write_text(_managed_yaml())
        dispatch_once(conn, spawn_fn=lambda task, *a, **k: launched.append(task.id))
        assert tid in launched
        assert len(kb.list_runs(conn, tid)) == 1
