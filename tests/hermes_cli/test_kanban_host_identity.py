"""Hostnames can change on wake; process ownership must not change with them."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_host_identity as identity


@pytest.fixture(autouse=True)
def clear_cache():
    clear = identity.boot_prefix.cache_clear
    clear()
    yield
    clear()


@pytest.mark.platforms("macos")
def test_boot_identity_survives_hostname_change_and_dispatcher_restart(monkeypatch):
    monkeypatch.setattr(identity.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='CB94B6BF-9036-41A6-8861-EF82D3222CDB\n'))
    monkeypatch.setattr(identity.socket, 'gethostname', lambda: 'before.local')
    lock = kb._claimer_id()
    monkeypatch.setattr(identity.socket, 'gethostname', lambda: 'after.local')
    identity.boot_prefix.cache_clear()  # fresh dispatcher's view
    assert kb._claim_is_local(lock)
    assert kb._claim_is_local('after.local:123')  # legacy compatibility
    assert not kb._claim_is_local('foreign.local:123')
    assert not kb._claim_is_local('boot-00000000-0000-0000-0000-000000000000:123')


@pytest.mark.platforms("macos")
@pytest.mark.parametrize('output', ['', 'not-a-uuid'])
def test_unavailable_identity_uses_hostname(monkeypatch, output):
    monkeypatch.setattr(identity.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=output))
    monkeypatch.setattr(identity.socket, 'gethostname', lambda: 'fallback')
    assert identity.claim_prefix() == 'fallback:'


def test_hostname_change_does_not_reclaim_live_run(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(identity, 'boot_prefix', lambda: 'boot-test:')
    monkeypatch.setattr(identity.socket, 'gethostname', lambda: 'before.local')
    path = tmp_path / 'board.db'
    kbc.init_db(path)
    with kbc.connect_closing(path) as conn:
        tid = kb.create_task(conn, title='survivor', assignee='default')
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        conn.execute('UPDATE tasks SET worker_pid=123, claim_expires=0 WHERE id=?', (tid,))
        conn.commit()
        monkeypatch.setattr(identity.socket, 'gethostname', lambda: 'after.local')
        monkeypatch.setattr(kb, '_worker_alive', lambda *a: True)
        assert kb.release_stale_claims(conn) == 0
        assert kb.get_task(conn, tid).current_run_id == task.current_run_id
        assert kb.get_task(conn, tid).status == 'running'
