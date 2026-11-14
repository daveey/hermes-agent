"""A census is owned by one explicit host pass, never by elapsed time."""
import pytest

from tui_gateway import kanban_unloaded_gates as gates
from tui_gateway import server


def test_scoped_census_reuses_success_and_rediscovers_next_pass(monkeypatch, tmp_path):
    profiles, calls = ['a'], []
    monkeypatch.setattr(server, '_readopt_wake_profiles',
                        lambda: calls.append(1) or [None, *profiles])
    monkeypatch.setattr(server, '_profile_home', lambda p: tmp_path / p)
    with gates.profile_census():
        first = gates._profile_paths()
        first.clear()  # callers cannot mutate the retained inventory
        assert tmp_path / 'a' / 'state.db' in gates._profile_paths()
        assert len(calls) == 1
    profiles[:] = ['b']
    with gates.profile_census():
        current = gates._profile_paths()
        assert tmp_path / 'b' / 'state.db' in current
        assert tmp_path / 'a' / 'state.db' not in current
    assert len(calls) == 2


def test_nested_scope_and_exception_restore_outer_census(monkeypatch, tmp_path):
    profiles, calls = ['outer'], []
    monkeypatch.setattr(server, '_readopt_wake_profiles',
                        lambda: calls.append(1) or [None, *profiles])
    monkeypatch.setattr(server, '_profile_home', lambda p: tmp_path / p)
    with gates.profile_census():
        outer = gates._profile_paths()
        profiles[:] = ['inner']
        with pytest.raises(RuntimeError), gates.profile_census():
            assert gates._profile_paths() != outer
            raise RuntimeError('work failed')
        assert gates._profile_paths() == outer
    assert gates._profile_paths() != outer
    assert len(calls) == 3


def test_host_pass_owns_census_and_unwinds(monkeypatch, tmp_path):
    from tui_gateway import kanban_unloaded as unloaded
    calls = []
    monkeypatch.setattr(server, '_readopt_wake_profiles', lambda: calls.append(1) or [None, 'a'])
    monkeypatch.setattr(server, '_profile_home', lambda p: tmp_path / p)
    monkeypatch.setattr(unloaded, 'live_session_ids', lambda: set())
    def run(name, fn, *args, **kwargs):
        gates._profile_paths()
        if name == 'guarantee_pass':
            raise ValueError('last pass failure')
    monkeypatch.setattr(gates, 'run', run)
    for _ in range(2):
        with pytest.raises(ValueError, match='last pass failure'):
            unloaded.host_pass()
        assert gates._profile_census.get() is None
    assert len(calls) == 2


def test_launch_only_discovery_is_not_retained(monkeypatch):
    # The existing discovery API suppresses filesystem errors into [None].
    # Without a success certificate that result must not be retained.
    calls = []
    monkeypatch.setattr(server, '_readopt_wake_profiles',
                        lambda: calls.append(1) or [None])
    with gates.profile_census():
        gates._profile_paths()
        gates._profile_paths()
    assert len(calls) == 2


def test_failed_discovery_is_not_retained_and_probe_stays_fail_open(monkeypatch):
    calls = []
    def discover():
        calls.append(1)
        raise OSError('unreadable profile inventory')
    monkeypatch.setattr(server, '_readopt_wake_profiles', discover)
    with gates.profile_census():
        assert gates.probe('delegation').pending
        assert gates.probe('killed_notices').pending
        assert gates.probe('guarantee_pass').pending
    assert len(calls) == 3
