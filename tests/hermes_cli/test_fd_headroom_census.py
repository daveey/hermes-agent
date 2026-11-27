"""Bounded native census without requiring a Darwin host."""
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hermes_cli import resource_limits as limits


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_census_classes(monkeypatch, platform):
    import fcntl
    targets = ['/.resource-leases/a', '/state.db', '/state.db-wal',
               '/state.db-shm (deleted)', 'socket:[123]', 'pipe:[456]', '/log']
    monkeypatch.setattr(limits.os, 'listdir', lambda _: [str(i) for i in range(7)])
    links = Mock(side_effect=lambda p: targets[int(p.rsplit('/', 1)[1])])
    monkeypatch.setattr(limits.os, 'readlink', links)
    monkeypatch.setattr(limits.os, 'fstat', lambda fd: SimpleNamespace(
        st_mode=stat.S_IFSOCK if fd == 4 else stat.S_IFIFO if fd == 5 else stat.S_IFREG))
    monkeypatch.setattr(fcntl, 'F_GETPATH', 50, raising=False)
    getpath = Mock(side_effect=lambda fd, op, buf: targets[fd].encode() + b'\x00')
    monkeypatch.setattr(fcntl, 'fcntl', getpath)
    count, census = limits._fd_snapshot(20, platform=platform)
    assert count == 7
    assert census['classes'] == {'resource-leases': 1, 'sqlite': 3, 'socket': 1, 'pipe': 1, 'other': 1}
    assert census['partial'] is False and census['coverage'] == 1
    if platform == 'darwin':
        links.assert_not_called()
        assert getpath.call_count == 5
    else:
        assert links.call_count == 7
        getpath.assert_not_called()


@pytest.mark.parametrize('budget', ['count', 'time'])
def test_census_stops_at_budget(monkeypatch, budget):
    monkeypatch.setattr(limits.os, 'listdir', lambda _: [str(i) for i in range(4000)])
    links = Mock(return_value='/log')
    monkeypatch.setattr(limits.os, 'readlink', links)
    clock = [0.0]
    def monotonic():
        clock[0] += .1 if budget == 'time' else 0
        return clock[0]
    count, census = limits._fd_snapshot(4096, clock=monotonic)
    assert count == 4000 and census['partial'] is True
    assert links.call_count <= (5 if budget == 'time' else 2000)
    assert census['coverage'] == census['examined'] / count


def test_below_quarter_does_no_census(monkeypatch):
    monkeypatch.setattr(limits.os, 'listdir', lambda _: ['0', '1'])
    links = Mock(side_effect=AssertionError('unneeded census'))
    monkeypatch.setattr(limits.os, 'readlink', links)
    assert limits._fd_snapshot(100) == (2, {})
    links.assert_not_called()
