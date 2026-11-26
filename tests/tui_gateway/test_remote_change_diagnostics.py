"""Stage-local receipts exercise the real remote fanout, never live stores."""
import contextlib
import json

import pytest

from hermes_cli import fact_links_health, kanban_session_activity as activity
from hermes_cli import loop_accounting as la
from tui_gateway import notice_fanout as nf, notice_fanout_state as state, notice_links_health


@pytest.fixture
def remote(monkeypatch, record_property):
    la.reset()
    controls = dict(previous={}, cards=[], health=None, seed=True, ack=True, prints={},
                    polls=[{'board': (set(), 10)}, {'board': (set(), 10)}], recovered=True,
                    skipped=None, refreshed=True, db=object(), calls=[])
    monkeypatch.setattr(nf, '_remote_memo', None)
    monkeypatch.setattr(activity, '_origin_scope', lambda: ('gw', 'default'))
    monkeypatch.setattr(fact_links_health, 'pending', lambda origin: controls['health'])
    monkeypatch.setattr(nf, '_remote_inputs_key', lambda: (('base',), []))
    monkeypatch.setattr(nf, '_ledger_content_key', lambda ledger: ())
    def poll(*args):
        controls['calls'].append('poll')
        result = controls['polls'].pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(nf, '_poll_boards', poll)
    monkeypatch.setattr(nf, '_remote_boards_quiet', lambda *args: True)
    monkeypatch.setattr(activity, '_remote_cards', lambda *a, **k: controls['cards'])
    monkeypatch.setattr(state, 'load_remote', lambda **k: controls['previous'])
    monkeypatch.setattr(state, 'seed_remote', lambda snapshot: controls['seed'])
    monkeypatch.setattr(state, 'ack_remote', lambda acked: controls['ack'])
    monkeypatch.setattr(notice_links_health, 'recover_into', lambda *a: controls['recovered'])
    def prints(*args):
        controls['calls'].append('prints')
        return controls['prints']
    monkeypatch.setattr(nf, '_known_prints', prints)
    monkeypatch.setattr(nf, 'chat_skip_reason', lambda *a: controls['skipped'])
    def refresh(*a, **k):
        controls['calls'].append('refresh')
        return controls['refreshed']
    monkeypatch.setattr('hermes_state_outcome.refresh_outcomes_quietly', refresh)
    monkeypatch.setattr(fact_links_health, 'acknowledge', lambda *a, **k: controls['calls'].append(('health', k['complete'])))
    @contextlib.contextmanager
    def opener(chat):
        yield controls['db'], chat
    controls['run'] = lambda: nf._remote_change_pass(opener)
    yield controls
    record_property('counter_snapshot', json.dumps(la.counters(), sort_keys=True))
    la.reset()


def runs(family):
    prefix = 'unloaded.diag.remote.' + family + '.'
    return {key[len(prefix):]: row['runs'] for key, row in la.counters().items()
            if key.startswith(prefix) and '.' not in key[len(prefix):]}


def conservation(chats=0, calls=1):
    for family in ('call', 'cert', 'health'):
        assert sum(runs(family).values()) == calls, (family, la.counters())
    for family in ('chat.trigger', 'chat.result'):
        assert sum(runs(family).values()) == chats, (family, la.counters())


@pytest.mark.parametrize(('after', 'cert'), [({'board': (set(), 10)}, 'certified'),
                                         (None, 'after_poll_none'), ({'board': (set(), 11)}, 'cursor_moved')])
def test_empty_projection_certification(remote, after, cert):
    remote['polls'][1] = after
    assert remote['run']() == []
    assert runs('cert') == {cert: 1}
    assert runs('cert.flag') == ({} if cert == 'certified' else {cert: 1})
    assert runs('call') == {'refreshed': 1}
    assert remote['calls'] == ['poll', 'prints', 'poll', ('health', True)]
    assert (nf._remote_memo is not None) == (cert == 'certified')
    conservation()


@pytest.mark.parametrize(('seed', 'health', 'terminal'), [(True, None, 'seed_only'),
                                                       (False, None, 'seed_failed'), (True, 1, 'refreshed')])
def test_seed_variants(remote, seed, health, terminal):
    remote.update(previous=None, seed=seed, health=health)
    remote['run']()
    assert runs('call') == {terminal: 1}
    assert runs('cert') == {('certified' if terminal == 'refreshed' else 'not_attempted'): 1}
    assert runs('health') == {('complete_true' if terminal == 'refreshed' else 'not_reached'): 1}
    conservation()


def test_memo_hit_short_circuit(remote):
    nf._remote_memo = (('base',), (), {'board': 10}, {})
    remote['run']()
    assert runs('call') == {'memo_hit': 1}
    assert runs('cert') == {'not_attempted': 1}
    assert runs('health') == {'not_reached': 1}
    assert remote['calls'] == ['poll']
    conservation()


@pytest.mark.parametrize(('kind', 'result'), [('ok', 'acked_refreshed'), ('skip', 'acked_intentionally_skipped'),
                                          ('db', 'db_none'), ('refresh', 'refresh_failed'), ('error', 'exception')])
def test_chat_results(remote, monkeypatch, kind, result):
    remote['cards'] = [dict(board='board', id='card', key='owner/board/card', status='blocked', session='chat')]
    if kind == 'skip':
        remote['skipped'] = 'archived'
    if kind == 'db':
        remote['db'] = None
    if kind == 'refresh':
        remote['refreshed'] = False
    if kind == 'error':
        def fail(*args):
            raise RuntimeError('chat failure')
        monkeypatch.setattr(nf, 'chat_skip_reason', fail)
    remote['run']()
    assert runs('chat.trigger') == {'snapshot_diff': 1}
    assert runs('chat.result') == {result: 1}
    assert remote['calls'].count('refresh') == (1 if kind in ('ok', 'refresh') else 0)
    assert runs('cert') == {('certified' if kind in ('ok', 'skip') else 'not_all_acked'): 1}
    conservation(chats=1)


@pytest.mark.parametrize('health', [None, 1])
def test_equal_chat_trigger_and_independent_health(remote, health):
    remote.update(previous={'chat': frozenset()}, health=health)
    # Absent versus empty are different in production, so use a real equal snapshot.
    remote['previous'] = {'chat': frozenset({('owner/board/card', 'blocked')})}
    remote['cards'] = [dict(board='board', id='card', key='owner/board/card', status='blocked', session='chat')]
    remote['run']()
    assert runs('chat.trigger') == {('unchanged_skip' if health is None else 'health_forced'): 1}
    assert runs('chat.result') == {('unchanged_skip' if health is None else 'acked_refreshed'): 1}
    assert runs('chat.flag') == ({} if health is None else {'health_pending': 1})
    assert runs('health') == {('no_generation' if health is None else 'complete_true'): 1}
    conservation(chats=1)


def test_prints_none_still_polls_and_preserves_overlapping_flags(remote):
    remote.update(prints=None)
    remote['polls'][1] = None
    remote['run']()
    assert runs('cert') == {'prints_none': 1}
    assert runs('cert.flag') == {'prints_none': 1, 'after_poll_none': 1}
    assert remote['calls'] == ['poll', 'prints', 'poll', ('health', True)]
    conservation()


def test_malformed_comparison_is_not_silenced_when_prints_present(remote):
    remote['polls'][1] = {}
    with pytest.raises(KeyError):
        remote['run']()
    assert runs('cert') == {'exception': 1}
    conservation()


@pytest.mark.parametrize('step', ['cards', 'load', 'ack', 'prints', 'poll', 'health'])
def test_exceptions_are_terminal_once(remote, monkeypatch, step):
    def fail(*a, **k):
        raise RuntimeError(step)
    targets = {'cards': (activity, '_remote_cards'), 'load': (state, 'load_remote'),
               'ack': (state, 'ack_remote'), 'prints': (nf, '_known_prints'),
               'health': (fact_links_health, 'acknowledge')}
    if step == 'cards':
        def cards(*a, **k):
            yield from ()
            raise RuntimeError(step)
        monkeypatch.setattr(activity, '_remote_cards', cards)
    elif step == 'poll':
        remote['polls'][1] = RuntimeError(step)
    else:
        target, name = targets[step]
        monkeypatch.setattr(target, name, fail)
    with pytest.raises(RuntimeError, match=step):
        remote['run']()
    assert runs('call') == {'exception': 1}
    assert runs('cert') == {('certified' if step == 'health' else 'exception'): 1}
    assert runs('health') == {('exception' if step == 'health' else 'not_reached'): 1}
    conservation()


@pytest.mark.parametrize(('failure', 'cert'), [('inputs', 'inputs_unavailable'), ('initial_poll', 'inputs_unavailable'),
                                            ('ack', 'ack_failed')])
def test_certification_precedence_and_input_failures(remote, monkeypatch, failure, cert):
    remote['health'] = 1 if failure == 'ack' else None
    if failure == 'inputs':
        def fail():
            raise OSError('input unavailable')
        monkeypatch.setattr(nf, '_remote_inputs_key', fail)
    if failure == 'initial_poll':
        remote['polls'][0] = None
    if failure == 'ack':
        remote['ack'] = False
    out = remote['run']()
    assert out.incomplete
    assert runs('cert') == {cert: 1}
    assert runs('call') == {'refreshed': 1}
    assert runs('miss.inputs_error') == {('true' if failure == 'inputs' else 'false'): 1}
    assert runs('miss.initial_poll_failed') == {('not_evaluated' if failure == 'inputs' else
                                              'true' if failure == 'initial_poll' else 'false'): 1}
    assert 'prints' not in remote['calls']
    if failure != 'ack':
        assert runs('miss.no_memo') == {'not_evaluated': 1}
    assert runs('health') == {('complete_false' if failure == 'ack' else 'no_generation'): 1}
    conservation()


@pytest.mark.parametrize('miss', ['health_pending', 'no_memo', 'inputs_changed', 'ledger_changed', 'boards_not_quiet'])
def test_evaluated_miss_and_short_circuit_receipts(remote, monkeypatch, miss):
    nf._remote_memo = (('base',), (), {'board': 10}, {})
    if miss == 'health_pending':
        remote['health'] = 1
    if miss == 'no_memo':
        nf._remote_memo = None
    if miss == 'inputs_changed':
        nf._remote_memo = (('old-base',), (), {'board': 10}, {})
    if miss == 'ledger_changed':
        nf._remote_memo = (('base',), ('old-ledger',), {'board': 10}, {})
    if miss == 'boards_not_quiet':
        monkeypatch.setattr(nf, '_remote_boards_quiet', lambda *a: False)
    remote['run']()
    chain = ['health_pending', 'no_memo', 'inputs_changed', 'ledger_changed', 'boards_not_quiet']
    for index, name in enumerate(chain):
        expected = 'false' if index < chain.index(miss) else 'true' if name == miss else 'not_evaluated'
        assert runs('miss.' + name) == {expected: 1}
    conservation()


@pytest.mark.parametrize('failure', ['db', 'recovery'])
def test_health_has_independent_failures(remote, failure):
    remote['health'] = 1
    remote['cards'] = [dict(key='key', status='running', session='chat')]
    remote['db'] = None if failure == 'db' else remote['db']
    remote['recovered'] = failure != 'recovery'
    remote['run']()
    assert runs('health') == {'complete_false': 1}
    assert runs('cert') == {('not_all_acked' if failure == 'db' else 'certified'): 1}
    conservation(1)


@pytest.mark.parametrize('reason', ['ledger_changed', 'boards_not_quiet'])
def test_evaluated_failure_is_unknown_not_unevaluated(remote, monkeypatch, reason):
    nf._remote_memo = (('base',), (), {'board': 10}, {})
    def fail(*a):
        raise OSError('evaluated')
    target = {'ledger_changed': '_ledger_content_key', 'boards_not_quiet': '_remote_boards_quiet'}[reason]
    monkeypatch.setattr(nf, target, fail)
    with pytest.raises(OSError, match='evaluated'):
        remote['run']()
    assert runs('miss.' + reason) == {'unknown': 1}
    conservation()


@pytest.mark.parametrize(('prints', 'after', 'cert', 'flag'), [
    (None, {'board': (set(), 11)}, 'prints_none', 'cursor_moved'),
    (None, {}, 'prints_none', 'unknown'),
    (None, {'board': (set(), 10)}, 'prints_none', None),
    ({}, {'board': (set(), 10), 'extra': (set(), 999)}, 'certified', None),
])
def test_simultaneous_flags_and_real_per_board_comparison(remote, prints, after, cert, flag):
    remote['prints'] = prints
    remote['polls'][1] = after
    remote['run']()
    assert runs('cert') == {cert: 1}
    if flag is not None:
        assert runs('cert.flag')[flag] == 1
    assert (nf._remote_memo is not None) == (cert == 'certified')
    conservation()


def test_changed_snapshot_and_moved_cursor_are_separate_units(remote):
    remote['cards'] = [dict(board='board', id='card', key='owner/board/card', status='blocked', session='chat')]
    remote['polls'][1] = {'board': (set(), 11)}
    remote['health'] = 1
    remote['run']()
    assert runs('chat.trigger') == {'snapshot_diff': 1}
    assert runs('chat.flag') == {'health_pending': 1}
    assert runs('cert') == {'cursor_moved': 1}
    conservation(chats=1)


def test_health_false_is_not_failed_memo_certification(remote):
    remote['health'] = 1
    remote['recovered'] = False
    remote['run']()
    assert runs('cert') == {'certified': 1}
    assert runs('health') == {'complete_false': 1}
    conservation()
