"""G2b: one config snapshot per blocker derivation, memoized run effects, fresh aliases."""
from hermes_cli import kanban_blocker_inputs as inputs
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_idle_candidates as idle
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


def _counting_config(monkeypatch, cfg):
    from hermes_cli import config
    calls = []

    def load():
        calls.append(1)
        return cfg
    monkeypatch.setattr(config, 'load_config_readonly', load)
    return calls


def test_derivation_loads_config_per_pass_not_per_run(kanban_home, monkeypatch):
    """The hub lanes spent most of their CPU on a cache-hit config load for every run row."""
    with kbc.connect_closing() as conn:
        for i in range(12):
            helpers._blocked_behind(conn, f'wall {i}', blocker_id='test-wall')
        calls = _counting_config(monkeypatch, {'kanban': {'shared_blocker_aliases': {}}})
        states = blockers.blocker_states(conn)
        assert len(states['test-wall'].behind) == 12
        # The snapshot itself plus each validated input read (tokens, check events, the G2c shared walk), each
        # keyed before and after: not one per row.
        assert len(calls) <= 7, len(calls)


def test_sweep_lanes_share_one_snapshot(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        for i in range(6):
            helpers._blocked_behind(conn, f'wall {i}', blocker_id='test-wall')
        calls = _counting_config(monkeypatch, {'kanban': {'shared_blocker_aliases': {}}})
        seen = []
        real = inputs.config

        def spy():
            seen.append(inputs._SCOPE.get())
            return real()
        monkeypatch.setattr(inputs, 'config', spy)

        class Result:
            pass
        idle.sweep_shared_blockers(conn, Result())
        # Every lane normalized inside one sweep scope (one snapshot, one effect memo).
        assert seen and all(s is not None and s is seen[0] for s in seen)
        assert inputs._SCOPE.get() is None
        del calls


def test_effect_memo_follows_alias_changes(kanban_home, monkeypatch):
    cfg = {'kanban': {'shared_blocker_aliases': {}}}
    _counting_config(monkeypatch, cfg)
    meta = '{"blocker_id": "old-wall"}'
    with kbc.connect_closing() as conn:
        @inputs.input_pass
        def exercise():
            assert blockers.run_effect('blocked', meta) == ('assert', 'old-wall')
            # Both an in-place edit and a replaced dict must reach the memo at the next validated read.
            cfg['kanban']['shared_blocker_aliases']['old-wall'] = 'new-wall'
            inputs.read('probe', conn, 10, lambda: 1)
            assert blockers.run_effect('blocked', meta) == ('assert', 'new-wall')
            monkeypatch.setattr('hermes_cli.config.load_config_readonly',
                                lambda: {'kanban': {'shared_blocker_aliases': {'old-wall': 'third-wall'}}})
            inputs.read('probe', conn, 10, lambda: 1)
            assert blockers.run_effect('blocked', meta) == ('assert', 'third-wall')
        exercise()


def test_outside_a_pass_config_is_fresh(kanban_home, monkeypatch):
    cfg = {'kanban': {'shared_blocker_aliases': {}}}
    calls = _counting_config(monkeypatch, cfg)
    meta = '{"blocker_id": "old-wall"}'
    assert blockers.run_effect('blocked', meta) == ('assert', 'old-wall')
    cfg['kanban']['shared_blocker_aliases']['old-wall'] = 'new-wall'
    assert blockers.run_effect('blocked', meta) == ('assert', 'new-wall')
    assert len(calls) == 2


def test_unavailable_config_is_not_retained(kanban_home, monkeypatch):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable
    state = {'down': True}

    def load():
        if state['down']:
            raise ConfigBackendUnavailable('down')
        return {'kanban': {'shared_blocker_aliases': {'old-wall': 'new-wall'}}}
    monkeypatch.setattr(config, 'load_config_readonly', load)

    @inputs.input_pass
    def exercise():
        assert blockers.normalize_blocker_id('old-wall') == 'old-wall'
        state['down'] = False
        assert blockers.normalize_blocker_id('old-wall') == 'new-wall'
    exercise()


def test_hold_decision_reads_config_once_without_deepcopy(kanban_home, monkeypatch):
    """The checks lookup and probe interval each deepcopied ``load_config()`` per held candidate (~14% of guard time)."""
    from hermes_cli import config
    deep = []
    with kbc.connect_closing() as conn:
        tids = [helpers._legacy_blocked_behind(conn, f'wall {i}', blocker_id='test-wall') for i in range(2)]
        st = blockers.blocker_states(conn)['test-wall']
        real_deep = config.load_config
        monkeypatch.setattr(config, 'load_config', lambda: deep.append(1) or real_deep())
        cfg = {'kanban': {'shared_blocker_checks': {'test-wall': 'exit 1'}}}
        calls = _counting_config(monkeypatch, cfg)
        seen = []   # the fixture pins the interval; record which config reached it
        monkeypatch.setattr(blockers, 'probe_interval_seconds', lambda c=None: seen.append(c) or 777)
        bid, sibling, due = blockers._shared_blocker_hold(conn, tids[1], now=min(st.behind.values()) + 1)
        assert (bid, sibling, due) == ('test-wall', None, min(st.behind.values()) + 777)
        assert seen == [cfg] and seen[0] is cfg and not deep
        # The checks lookup saw the same read: a registered wall honours this card's own release
        # even while a sibling's probe runs; an unregistered one keeps it behind that probe.
        assert helpers.kb.unblock_task(conn, tids[0]) and helpers.kb.claim_task(conn, tids[0])
        assert helpers.kb.unblock_task(conn, tids[1])
        deep.clear()
        later = min(st.behind.values()) + 2
        assert blockers._shared_blocker_hold(conn, tids[1], now=later) is None
        _counting_config(monkeypatch, {'kanban': {'shared_blocker_probe_seconds': 777}})
        assert blockers._shared_blocker_hold(conn, tids[1], now=later) == ('test-wall', tids[0], 0)
        assert not deep and calls


def test_non_mapping_kanban_section_does_not_crash_derivation(kanban_home, monkeypatch):
    """A scalar/list ``kanban`` section is fail-closed config, not a crash in the identity read."""
    for section in ('oops', [1, 2], 7):
        _counting_config(monkeypatch, {'kanban': section})
        with kbc.connect_closing() as conn:
            assert blockers.blocker_states(conn) == {}
        assert inputs._aliases({'kanban': section}) == {}


def _backend_down(monkeypatch):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable

    def down(*_args, **_kwargs):
        raise ConfigBackendUnavailable('down')
    monkeypatch.setattr(config, 'load_config_readonly', down)
    monkeypatch.setattr(config, 'load_config', down)


def _count_hold_reads(monkeypatch):
    """Reads of the pass snapshot made by the hold decision's own helper (state derivation and alias
    normalization read it for themselves)."""
    import sys
    real, reads = inputs.config, []

    def spy():
        if sys._getframe(1).f_code.co_name == '_hold_config':
            reads.append(1)
        return real()
    monkeypatch.setattr(inputs, 'config', spy)
    return reads


def test_hold_config_reads_once_and_returns_that_read(kanban_home, monkeypatch):
    cfg = {'kanban': {}}
    calls = _counting_config(monkeypatch, cfg)
    assert blockers._hold_config() is cfg
    assert len(calls) == 1, len(calls)


def test_hold_decision_makes_exactly_one_hold_config_read(kanban_home, monkeypatch):
    """A successful hold decision shares one read between the checks lookup and the probe interval:
    ``calls`` being non-empty (as above) would also pass with two or three reads."""
    with kbc.connect_closing() as conn:
        tids = [helpers._legacy_blocked_behind(conn, f'wall {i}', blocker_id='test-wall') for i in range(2)]
        st = blockers.blocker_states(conn)['test-wall']
        now = min(st.behind.values()) + 1
        _counting_config(monkeypatch, {'kanban': {'shared_blocker_checks': {'test-wall': 'exit 1'}}})
        monkeypatch.setattr(blockers, 'probe_interval_seconds', lambda c=None: 777)
        reads = _count_hold_reads(monkeypatch)
        assert blockers._shared_blocker_hold(conn, tids[1], now=now) == ('test-wall', None, min(st.behind.values()) + 777)
        assert len(reads) == 1, len(reads)
        # The early return (registered wall honours the operator's release) takes the same single read.
        assert helpers.kb.unblock_task(conn, tids[0]) and helpers.kb.claim_task(conn, tids[0])
        assert helpers.kb.unblock_task(conn, tids[1])
        reads.clear()
        assert blockers._shared_blocker_hold(conn, tids[1], now=now + 1) is None
        assert len(reads) == 1, len(reads)


def test_hold_decision_falls_back_when_config_backend_is_unavailable(kanban_home, monkeypatch):
    """A down config backend must not escape the hold decision: the readers take their own fallbacks
    (no registered checks, the interval reader gets ``None``), exactly as before the shared read."""
    from hermes_cli.config_backend import ConfigBackendUnavailable

    def outcome(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConfigBackendUnavailable as exc:   # a value, so a leaked backend error fails an assertion below
            return f'raised {exc!r}'
    with kbc.connect_closing() as conn:
        tids = [helpers._legacy_blocked_behind(conn, f'wall {i}', blocker_id='test-wall') for i in range(2)]
        st = blockers.blocker_states(conn)['test-wall']
        now = min(st.behind.values()) + 1
        healthy = {'kanban': {'shared_blocker_checks': {'test-wall': 'exit 1'}}}
        _counting_config(monkeypatch, healthy)
        seen = []
        monkeypatch.setattr(blockers, 'probe_interval_seconds', lambda c=None: seen.append(c) or 777)
        due = min(st.behind.values()) + 777
        assert outcome(blockers._shared_blocker_hold, conn, tids[1], now=now) == ('test-wall', None, due)
        assert seen == [healthy]
        # Down: the same decision, with the interval reader falling back on its own load.
        seen.clear()
        _backend_down(monkeypatch)
        assert outcome(blockers._shared_blocker_hold, conn, tids[1], now=now) == ('test-wall', None, due)
        assert seen == [None]
        assert outcome(blockers.shared_blocker_guard, conn, tids[1], now=now) == blockers.GUARD_REASON
        assert outcome(blockers._hold_config) is None
        # Checks cannot be listed either, so a registered wall's operator-release exemption does not apply.
        assert helpers.kb.unblock_task(conn, tids[0]) and helpers.kb.claim_task(conn, tids[0])
        assert helpers.kb.unblock_task(conn, tids[1])
        assert outcome(blockers._shared_blocker_hold, conn, tids[1], now=now + 1) == ('test-wall', tids[0], 0)
        _counting_config(monkeypatch, healthy)
        assert outcome(blockers._shared_blocker_hold, conn, tids[1], now=now + 1) is None
