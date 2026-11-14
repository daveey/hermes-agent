"""G2b follow-up: one live config read per validated input read, same alias freshness."""
from hermes_cli import kanban_blocker_inputs as inputs
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home


def _loader(monkeypatch, cfg):
    calls = []

    def load():
        calls.append(1)
        return cfg[0]
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', load)
    return calls


def test_cache_miss_reads_live_config_once(kanban_home, monkeypatch):
    """The post-compute check trusts the pass snapshot: compute only sees config through it."""
    calls = _loader(monkeypatch, [{'kanban': {'shared_blocker_aliases': {}}}])
    with kbc.connect_closing() as conn:
        @inputs.input_pass
        def exercise():
            inputs.config()
            before = len(calls)
            assert inputs.read('probe', conn, 10, lambda: 1) == 1   # miss
            assert inputs.read('probe', conn, 10, lambda: 2) == 1   # hit
            return len(calls) - before
        assert exercise() == 2


def test_in_place_edit_during_compute_is_not_cached(kanban_home, monkeypatch):
    """A value computed after the snapshot's aliases changed must not be stored under the old
    aliases, or reverting them would replay it."""
    cfg = {'kanban': {'shared_blocker_aliases': {}}}
    _loader(monkeypatch, [cfg])
    aliases = cfg['kanban']['shared_blocker_aliases']
    with kbc.connect_closing() as conn:
        @inputs.input_pass
        def exercise():
            inputs.config()

            def edit():
                aliases['old-wall'] = 'new-wall'
                return 'under-new'
            assert inputs.read('probe', conn, 10, edit) == 'under-new'
            del aliases['old-wall']
            return inputs.read('probe', conn, 10, lambda: 'under-old')
        assert exercise() == 'under-old'


def test_replaced_config_during_compute_reaches_the_next_read(kanban_home, monkeypatch):
    held = [{'kanban': {'shared_blocker_aliases': {}}}]
    _loader(monkeypatch, held)
    with kbc.connect_closing() as conn:
        @inputs.input_pass
        def exercise():
            inputs.config()

            def swap():
                held[0] = {'kanban': {'shared_blocker_aliases': {'old-wall': 'new-wall'}}}
                return 'first'
            assert inputs.read('probe', conn, 10, swap) == 'first'
            value = inputs.read('probe', conn, 10, lambda: 'second')
            return value, inputs.config() is held[0]
        assert exercise() == ('second', True)


def test_no_snapshot_post_check_reads_live(kanban_home, monkeypatch):
    """Without a held snapshot the compute may have taken one from a newer live config."""
    held = [{'kanban': {'shared_blocker_aliases': {}}}]
    _loader(monkeypatch, held)
    with kbc.connect_closing() as conn:
        @inputs.input_pass
        def exercise():
            def swap_then_snapshot():
                held[0] = {'kanban': {'shared_blocker_aliases': {'old-wall': 'new-wall'}}}
                inputs.config()
                return 'under-new'
            assert inputs.read('probe', conn, 10, swap_then_snapshot) == 'under-new'
            held[0] = {'kanban': {'shared_blocker_aliases': {}}}
            return inputs.read('probe', conn, 10, lambda: 'under-old')
        assert exercise() == 'under-old'
