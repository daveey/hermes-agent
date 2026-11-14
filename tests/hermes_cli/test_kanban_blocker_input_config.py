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
        # The snapshot itself plus each validated input read (tokens, check events): not one per row.
        assert len(calls) <= 6, len(calls)


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


def test_non_mapping_kanban_section_does_not_crash_derivation(kanban_home, monkeypatch):
    """A scalar/list ``kanban`` section is fail-closed config, not a crash in the identity read."""
    for section in ('oops', [1, 2], 7):
        _counting_config(monkeypatch, {'kanban': section})
        with kbc.connect_closing() as conn:
            assert blockers.blocker_states(conn) == {}
        assert inputs._aliases({'kanban': section}) == {}
