"""A1–A5: a proven absent destination is an ACK, not a one-second obligation."""
import contextlib
import logging

import pytest

from hermes_cli import fact_links_health
from tests.tui_gateway import test_remote_change_diagnostics as helpers
from tui_gateway import kanban_unloaded as ku, notice_fanout as nf, notice_fanout_state as state

remote = helpers.remote


class Lookup(tuple):
    def __new__(cls, db=None, tip=None, *, unavailable=False):
        result = super().__new__(cls, (db, tip))
        result.unavailable = unavailable
        return result


@pytest.fixture
def absent(remote, monkeypatch):
    nf._reset_for_tests()
    clock = [1000.25]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    controls = {'available': False, 'unavailable': False, 'opened': [], 'acks': [], 'clock': clock}
    remote['cards'] = [dict(board='board', id='card', key='owner/board/card', status='blocked', session='missing')]
    remote['polls'] = [{'board': (set(), 10)}] * 30

    @contextlib.contextmanager
    def opener(chat):
        controls['opened'].append(chat)
        yield Lookup(remote['db'] if controls['available'] or chat == 'present' else None,
                     chat, unavailable=controls['unavailable'])

    def ack(acked):
        controls['acks'].append(dict(acked))
        for chat, cards in acked.items():
            if cards is None:
                remote['previous'].pop(chat, None)
            else:
                remote['previous'][chat] = cards
        return True

    monkeypatch.setattr(state, 'ack_remote', ack)
    controls['run'] = lambda: nf._remote_change_pass(opener)
    controls['remote'] = remote
    return controls


def test_A1_proven_absence_acked_certified_warning_once(absent, caplog):
    with caplog.at_level(logging.WARNING, logger=nf.__name__):
        result = absent['run']()
        assert not result.incomplete
        assert [(row['chat_id'], row['skipped']) for row in result] == [('missing', 'no_local_session')]
        assert absent['acks'] == [{'missing': frozenset({('owner/board/card', 'blocked')})}]
        assert helpers.runs('cert') == {'certified': 1}
        assert helpers.runs('chat.result') == {'acked_intentionally_skipped': 1}
        assert absent['remote']['calls'].count('refresh') == 0
        absent['run']()
        absent['clock'][0] += 60
        absent['run']()
    warnings = [r for r in caplog.records if r.name == nf.__name__ and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'missing' in warnings[0].getMessage()


def test_A2_store_unavailable_retains_retry_without_ack(absent):
    absent['unavailable'] = True
    result = absent['run']()
    assert result.incomplete
    assert absent['acks'] == [{}]
    assert helpers.runs('chat.result') == {'db_none': 1}
    assert helpers.runs('cert') == {'not_all_acked': 1}
    assert absent['remote']['calls'].count('refresh') == 0


def test_A3_later_session_only_rechecked_at_maintenance(absent):
    assert not absent['run']().incomplete
    absent['available'] = True
    absent['clock'][0] += 1
    absent['run']()
    absent['clock'][0] += 58.999
    absent['run']()
    assert absent['opened'] == ['missing']
    assert absent['remote']['calls'].count('refresh') == 0
    absent['clock'][0] = 1060.25
    # Elapsed time alone must not turn an unrelated retry into maintenance.
    from tui_gateway.unloaded_diagnostics import Admission
    with Admission(1060.25, deadlines=[('retry', 1060.25, 'remote_notices', 'unknown')]):
        assert not absent['run']().incomplete
    assert absent['opened'] == ['missing']
    with Admission(1060.25, maintenance=True):
        assert not absent['run']().incomplete
    assert absent['opened'] == ['missing', 'missing']
    assert absent['remote']['calls'].count('refresh') == 1


def test_A4_snapshot_change_rechecks_before_maintenance(absent, caplog):
    absent['run']()
    absent['remote']['cards'][0]['status'] = 'running'
    # A real input invalidation makes the remote memo miss.
    nf._remote_memo = None
    absent['clock'][0] += 1
    with caplog.at_level(logging.WARNING, logger=nf.__name__):
        result = absent['run']()
    assert not result.incomplete
    assert absent['opened'] == ['missing', 'missing']
    assert absent['acks'][-1] == {'missing': frozenset({('owner/board/card', 'running')})}
    assert len([r for r in caplog.records if r.name == nf.__name__ and r.levelno == logging.WARNING]) == 2


def test_A5_absence_does_not_force_other_chat_on_two_retry_passes(absent, monkeypatch):
    remote = absent['remote']
    remote['cards'][0]['origin_session'] = 'present'
    remote['health'] = 1
    def health_ack(origin, generation, *, complete):
        if complete:
            remote['health'] = None
    monkeypatch.setattr(fact_links_health, 'acknowledge', health_ack)
    assert not absent['run']().incomplete
    assert remote['health'] is None
    # Force ordinary projection passes, as if unrelated host work invalidated the memo.
    for _ in range(2):
        absent['clock'][0] += 1
        nf._remote_memo = None
        assert not absent['run']().incomplete
    assert absent['opened'] == ['missing', 'present']
    assert remote['calls'].count('refresh') == 1
    assert helpers.runs('chat.trigger') == {'snapshot_diff': 2, 'unchanged_skip': 4}
    assert helpers.runs('health') == {'complete_true': 1, 'no_generation': 2}


def test_new_health_generation_uses_cached_absence_without_lookup(absent):
    absent['run']()
    absent['clock'][0] += 1
    absent['remote']['health'] = 2
    assert not absent['run']().incomplete
    assert absent['opened'] == ['missing']
    assert absent['acks'][-1] == {'missing': frozenset({('owner/board/card', 'blocked')})}
    assert helpers.runs('health') == {'no_generation': 1, 'complete_true': 1}


@pytest.mark.parametrize('failure', ['raise', 'none', 'healthy'])
def test_A2_real_finder_reports_open_failures(monkeypatch, failure):
    from tui_gateway import server
    monkeypatch.setattr('tui_gateway.kanban_chat_hosting.profile_roster', lambda: ([None, 'second'], True))
    class Store:
        def get_session(self, chat):
            return None
    @contextlib.contextmanager
    def profile_db(scope, **kwargs):
        if scope is None and failure == 'raise':
            raise OSError('store unavailable')
        yield None if scope is None and failure == 'none' else Store()
    monkeypatch.setattr(server, '_profile_db', profile_db)
    with ku.find_session_db('missing') as lookup:
        assert tuple(lookup) == (None, None)
        assert lookup.unavailable is (failure != 'healthy')
