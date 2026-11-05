"""Authority, identity and uncertainty controls for the read-only cleanup veto."""
from unittest.mock import Mock

import psutil
import pytest

from hermes_cli import kanban_workspace_jobs as jobs


@pytest.fixture
def authority(tmp_path, monkeypatch):
    proc = Mock(pid=987654321)
    proc.username.return_value = 'fixture-user'
    proc.cwd.return_value = str(tmp_path)
    proc.environ.return_value = {'HERMES_OWNER_CARD': 't_abcdef01@fixture-host'}
    proc.status.return_value = psutil.STATUS_SLEEPING
    monkeypatch.setattr(jobs, 'get_process_start_time', lambda pid: 'boot:100')
    monkeypatch.setattr(jobs, 'hostname_prefix', lambda: 'fixture-host:')
    monkeypatch.setattr(psutil, 'Process', lambda: proc)
    monkeypatch.setattr(psutil, 'process_iter', lambda: iter([proc]))
    return proc, tmp_path


@pytest.mark.parametrize('tag', ['t_abcd1234@fixture-host', 't_abcdef01@FIXTURE-HOST.example'])
def test_local_shared_owner_or_fqdn_holds(authority, tag):
    proc, path = authority
    proc.environ.return_value = {'HERMES_OWNER_CARD': tag}
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)


def test_fqdn_hostname_accepts_short_tag(authority, monkeypatch):
    _, path = authority
    monkeypatch.setattr(jobs, 'hostname_prefix', lambda: 'FIXTURE-HOST.example:')
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)


def test_exact_live_owner_holds(authority):
    _, path = authority
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)


@pytest.mark.parametrize('method,value', [
    ('username', 'another-user'),
    ('environ', {'HERMES_OWNER_CARD': 't_other@fixture-host'}),
    ('environ', {'HERMES_OWNER_CARD': 't_abcdef01@foreign-host'}),
    ('environ', {}),
    ('cwd', ''),
    ('status', psutil.STATUS_ZOMBIE),
    ('status', psutil.STATUS_DEAD),
])
def test_non_authorities_do_not_hold(authority, monkeypatch, method, value):
    proc, path = authority
    monkeypatch.setattr(psutil, 'Process', lambda: Mock(username=lambda: 'fixture-user'))
    getattr(proc, method).return_value = value
    assert not jobs.defer_owned_job_cleanup('t_abcdef01', path)


@pytest.mark.parametrize('location', ['outside', 'prefix-sibling'])
def test_unrelated_cwd_does_not_hold(authority, location):
    proc, path = authority
    proc.cwd.return_value = {'outside': str(path.parent), 'prefix-sibling': str(path) + '-other'}[location]
    assert not jobs.defer_owned_job_cleanup('t_abcdef01', path)


def test_vanished_process_does_not_hold(authority):
    proc, path = authority
    proc.environ.side_effect = psutil.NoSuchProcess(proc.pid)
    assert not jobs.defer_owned_job_cleanup('t_abcdef01', path)


def test_reused_pid_does_not_hold(authority, monkeypatch):
    _, path = authority
    stamps = iter(['boot:100', 'boot:200'])
    monkeypatch.setattr(jobs, 'get_process_start_time', lambda pid: next(stamps))
    assert not jobs.defer_owned_job_cleanup('t_abcdef01', path)


@pytest.mark.parametrize('stamps', [(None, 'boot:100'), ('boot:100', None)])
def test_matching_candidate_unknown_birth_defers(authority, monkeypatch, stamps):
    _, path = authority
    values = iter(stamps)
    monkeypatch.setattr(jobs, 'get_process_start_time', lambda pid: next(values))
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)


@pytest.mark.parametrize('stage,expected', [('cwd', False), ('environ', True)])
def test_access_denial_requires_known_path_dependency(authority, stage, expected):
    proc, path = authority
    getattr(proc, stage).side_effect = psutil.AccessDenied(proc.pid)
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path) is expected


def test_scan_budget_defers_without_process_read(authority, monkeypatch):
    proc, path = authority
    ticks = iter([10.0, 12.0])
    monkeypatch.setattr(jobs.time, 'monotonic', lambda: next(ticks))
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)
    proc.cwd.assert_not_called()


def test_discovery_failure_defers(authority, monkeypatch):
    _, path = authority
    def broken():
        raise OSError('unavailable')
    monkeypatch.setattr(psutil, 'process_iter', broken)
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)


@pytest.mark.parametrize('kind', ['file', 'symlink', 'absent'])
def test_non_directory_never_authorizes_deletion(authority, kind):
    proc, path = authority
    target = path / 'target'
    if kind == 'file':
        target.write_text('not a directory')
    elif kind == 'symlink':
        target.symlink_to(path, target_is_directory=True)
    assert jobs.defer_owned_job_cleanup('t_abcdef01', target)
    proc.cwd.assert_not_called()


def test_descendant_cwd_holds(authority):
    proc, path = authority
    proc.cwd.return_value = str(path / 'nested')
    assert jobs.defer_owned_job_cleanup('t_abcdef01', path)


def test_empty_inventory_allows_cleanup(authority, monkeypatch):
    _, path = authority
    monkeypatch.setattr(psutil, 'process_iter', lambda: iter([]))
    assert not jobs.defer_owned_job_cleanup('t_abcdef01', path)
