"""Recovery must not depend on the last worker to repeat a shared wall."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home

ALIASES = ('github-rest-quota', 'github-rest-quota-reserve',
           'github-shared-rest-quota', 'github-quota-reserve')


@pytest.mark.parametrize('alias', (*ALIASES, 'github-core-shared-rest-quota-reserve'))
def test_github_wall_has_one_identity(alias):
    assert blockers.normalize_blocker_id(alias) == 'github-rest-quota'


def test_new_assertions_do_not_postpone_oldest_probe(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tids = []
        start = 1800000000
        for step in range(7):
            monkeypatch.setattr(blockers.time, 'time', lambda step=step: start + step * 300)
            tids.append(helpers._legacy_blocked_behind(conn, f'waiter {step}', blocker_id='test-wall'))
        assert blockers.shared_blocker_guard(conn, tids[0], now=start + 1799, interval=1800)
        assert blockers.shared_blocker_guard(conn, tids[0], now=start + 1800, interval=1800) is None
        assert blockers.probe_stranded_blockers(conn, now=start + 1800, interval=1800) == [tids[0]]


def test_dispatch_tick_clears_all_aliases_and_carrier(kanban_home, monkeypatch):
    from hermes_cli import config, kanban_idle_candidates

    monkeypatch.setattr(config, 'load_config', lambda: {
        'kanban': {'shared_blocker_checks': {'github-rest-quota': 'exit 0'}}})
    with kbc.connect_closing() as conn:
        tids = [helpers._blocked_behind(conn, alias, blocker_id=alias) for alias in ALIASES]
        result = SimpleNamespace()
        kanban_idle_candidates.sweep_shared_blockers(conn, result)
        assert set(result.shared_blocker_released) == set(tids)
        assert all(kb.get_task(conn, tid).status == 'ready' for tid in tids)
        state = blockers.blocker_states(conn)['github-rest-quota']
        assert not state.is_open
        assert state.cleared_via == 'check'
        assert not blockers.render_blocker_context(conn, tids[0])
        for tid in tids:
            events = [e for e in kb.list_events(conn, tid) if e.kind == blockers.CLEARED_EVENT]
            assert events[-1].payload['cleared_via'] == 'check'


def test_non_probe_context_requires_live_check(kanban_home):
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, 'quota', blocker_id='github-quota-reserve')
        text = blockers.render_blocker_context(conn, 'unrelated-card')
        assert '--reserve-ok' in text
        assert 'Never re-assert without a live check' in text
        assert 'instead of rediscovering' not in text
