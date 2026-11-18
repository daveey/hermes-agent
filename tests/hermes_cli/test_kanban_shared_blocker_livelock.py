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


def test_config_alias_and_legacy_event_reader(kanban_home, monkeypatch):
    from hermes_cli import config
    cfg = {'kanban': {'shared_blocker_aliases': {'quota-limited': 'github-quota-reserve'}}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: cfg)
    assert blockers.normalize_blocker_id('quota-limited') == ALIASES[0]
    assert blockers.normalize_blocker_id('unknown-wall') == 'unknown-wall'
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_behind(conn, 'wall', blocker_id=ALIASES[0])
        assert kb.unblock_task(conn, tid)
        assert blockers._unblocked_since_assertion(conn, tid, ALIASES[0])
        # Persist an old spelling, not the current writer's normalized spelling.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'scheduled', {'blocker_id': ALIASES[2]})
        assert not blockers._unblocked_since_assertion(conn, tid, ALIASES[0])


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


@pytest.mark.parametrize('via_check', [False, True])
def test_pre_clear_run_cannot_reopen_quota_wall(kanban_home, monkeypatch, via_check):
    from hermes_cli import kanban_shared_blocker_checks as checks

    clock = [1800000000]
    monkeypatch.setattr(blockers.time, 'time', lambda: clock[0])
    with kbc.connect_closing() as conn:
        carrier = helpers._blocked_behind(conn, 'carrier', blocker_id=ALIASES[0])
        stale = kb.create_task(conn, title='already running with old context', assignee='default')
        assert kb.claim_task(conn, stale)
        clock[0] += 30
        if via_check:
            checks.run_due_checks(conn, cfg={'kanban': {'shared_blocker_checks': {ALIASES[0]: 'exit 0'}}})
        else:
            assert kb.unblock_task(conn, carrier)
            assert kb.claim_task(conn, carrier)
            assert kb.block_task(conn, carrier, reason='other wait', kind='time', clear_blocker=True, clock_only=True)
        cleared_at = clock[0]
        clock[0] += 30
        assert kb.block_task(conn, stale, reason='stale quota context', kind='capability', blocker_id=ALIASES[2])
        state = blockers.blocker_states(conn)[ALIASES[0]]
        assert not state.is_open
        assert state.cleared_at == cleared_at
        assert stale in blockers.release_cleared_blockers(conn)
        # A genuinely new run after the clear may still report a new outage.
        clock[0] += 30
        helpers._blocked_behind(conn, 'new outage', blocker_id=ALIASES[3])
        assert blockers.blocker_states(conn)[ALIASES[0]].is_open


def test_non_probe_context_requires_live_check(kanban_home):
    with kbc.connect_closing() as conn:
        helpers._blocked_behind(conn, 'quota', blocker_id='github-quota-reserve')
        text = blockers.render_blocker_context(conn, 'unrelated-card')
        assert '--reserve-ok' in text
        assert 'Never re-assert without a live check' in text
        assert 'instead of rediscovering' not in text


@pytest.mark.parametrize('with_checked_wall', [False, True])
@pytest.mark.parametrize('is_probe', [False, True])
def test_unchecked_wall_requires_this_runs_refusal(kanban_home, with_checked_wall, is_probe):
    with kbc.connect_closing() as conn:
        carrier = helpers._blocked_behind(conn, 'old outage', blocker_id='unchecked-provider-wall')
        if with_checked_wall:
            helpers._blocked_behind(conn, 'quota', blocker_id='github-quota-reserve')
        text = blockers.render_blocker_context(conn, carrier if is_probe else 'unrelated-card')
        assert 'The listing is not evidence' in text
        assert "try it, or run the provider's own free read" in text
        assert 'only if that attempt in this run was refused' in text
        assert 'quote the refusal in the reason' in text
        assert 'stop at the affected step and end with the same blocker_id' not in text
        assert 'instead of rediscovering' not in text
        if with_checked_wall:
            assert 'Never re-assert without a live check' in text


def test_promoted_card_after_reset_checks_before_continuing(kanban_home, monkeypatch):
    from hermes_cli import config, kanban_shared_blocker_checks as checks

    clock = [1800000000]
    monkeypatch.setattr(blockers.time, 'time', lambda: clock[0])
    # The controlled checker represents the companion ledger after its reset.
    cfg = {'kanban': {'shared_blocker_checks': {'github-rest-quota': 'exit 0'}}}
    monkeypatch.setattr(config, 'load_config', lambda: cfg)
    with kbc.connect_closing() as conn:
        carrier = helpers._blocked_behind(conn, 'before reset', blocker_id=ALIASES[2])
        parent = kb.create_task(conn, title='review', assignee='default')
        child = kb.create_task(conn, title='landing needs GitHub', assignee='default', parents=[parent])
        assert kb.get_task(conn, child).status == 'todo'
        clock[0] += 120
        assert kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, result='review done')
        assert kb.get_task(conn, child).status == 'ready'
        assert child not in blockers.blocker_states(conn)[ALIASES[0]].behind
        text = blockers.render_blocker_context(conn, child)
        assert 'exit 0' in text and 'Never re-assert without a live check' in text
        assert 'instead of rediscovering' not in text
        # Execute the rendered policy's command, not a fresh blocker assertion.
        assert checks._run_check(checks.registered_checks(cfg)[ALIASES[0]])[0] == 0
        assert checks.run_due_checks(conn, cfg=cfg) == [ALIASES[0]]
        assert blockers.release_cleared_blockers(conn) == [carrier]
        assert kb.claim_task(conn, child)
        assert kb.complete_task(conn, child, result='continued after cheap check')
        assert not blockers.blocker_states(conn)[ALIASES[0]].is_open
