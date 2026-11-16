"""Focused list reads must not reopen the ledger's writer/bootstrap path."""
import sqlite3

import pytest

from tools import async_delegation as ad
from tests.tools.test_async_delegation_failed_surface import home as _home_fixture, _delegation

home = _home_fixture


def test_focused_read_never_opens_writer_after_dispatch(monkeypatch):
    _delegation('failed', status='error', result={'status': 'error'}, completed_at=1000)
    def forbidden():
        raise AssertionError('focused list reopened writer/bootstrap')
    monkeypatch.setattr(ad, '_connect', forbidden)
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['delegation_id'] == 'failed'


def test_unchanged_reads_and_due_time_do_not_rescan(home, monkeypatch):
    from tools import async_delegation_focused_reads as reads
    _delegation('failed', status='error', result={'status': 'error'}, completed_at=1000)
    statements = []
    connect = reads.sqlite3.connect
    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(reads.sqlite3, 'connect', traced)
    deadline = 1000 + ad._FAILURE_SURFACE_WINDOW_S
    for now in (1001, deadline):
        assert ad.failed_delegations_for_session('ui-1', now=now)
    assert ad.failed_delegations_for_session('ui-1', now=deadline + 0.25) == []
    # Wall-clock rollback restores the still-valid row, without a database commit.
    assert ad.failed_delegations_for_session('ui-1', now=1001)
    assert len([s for s in statements if s.startswith('SELECT delegation_id')]) == 1


def test_missing_store_is_not_created(home):
    assert ad.failed_delegations_for_session('ui-1') == []
    assert not (home / 'state.db').exists()


def test_external_commits_refresh_only_requested_scope(home):
    _delegation('failed', status='error', result={'status': 'error'}, completed_at=1000)
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['status'] == 'error'
    assert ad.failed_delegations_for_session('other', 'other', now=1001) == []
    # Separate real writer, not a same-handle total_changes approximation.
    with sqlite3.connect(home / 'state.db') as writer:
        writer.execute("UPDATE async_delegations SET result_json=? WHERE delegation_id='failed'",
                       ('{"status":"timeout","error":"new error"}',))
    row = ad.failed_delegations_for_session('ui-1', now=1001)[0]
    assert (row['status'], row['error']) == ('timeout', 'new error')
    with sqlite3.connect(home / 'state.db') as writer:
        writer.execute("UPDATE async_delegations SET origin_ui_session_id='other', parent_session_id='other'")
    assert ad.failed_delegations_for_session('ui-1', 'agent-1', now=1001) == []
    assert ad.failed_delegations_for_session('other', 'other', now=1001)[0]['delegation_id'] == 'failed'
    with sqlite3.connect(home / 'state.db') as writer:
        writer.execute('DELETE FROM async_delegations')
    assert ad.failed_delegations_for_session('other', now=1001) == []


def test_failed_row_appears_after_empty_snapshot():
    ad._persist_dispatch({'delegation_id': 'later', 'origin_ui_session_id': 'ui-1', 'session_key': '', 'dispatched_at': 900})
    assert ad.failed_delegations_for_session('ui-1', now=1001) == []
    ad._persist_completion({'delegation_id': 'later', 'status': 'error', 'completed_at': 1000}, {'status': 'error'})
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['delegation_id'] == 'later'


def test_profiles_and_limits_are_separate(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    _delegation('one', status='error', result={'status': 'error'}, completed_at=1000)
    _delegation('two', status='error', result={'status': 'error'}, completed_at=1001)
    assert len(ad.failed_delegations_for_session('ui-1', now=1002, limit=1)) == 1
    assert len(ad.failed_delegations_for_session('ui-1', now=1002, limit=2)) == 2
    token = set_hermes_home_override(str(home / 'other'))
    try:
        assert ad.failed_delegations_for_session('ui-1', now=1002) == []
        _delegation('other-home', status='error', result={'status': 'error'}, completed_at=1000)
        assert ad.failed_delegations_for_session('ui-1', now=1002)[0]['delegation_id'] == 'other-home'
    finally:
        reset_hermes_home_override(token)
    assert ad.failed_delegations_for_session('ui-1', now=1002)[0]['delegation_id'] == 'two'


@pytest.mark.parametrize('during_query', [False, True])
def test_atomic_replacement_never_reuses_old_rows(home, monkeypatch, during_query):
    from tools import async_delegation_focused_reads as reads
    _delegation('old', status='error', result={'status': 'error'}, completed_at=1000)
    path = home / 'state.db'
    # Independent standalone copy; backup includes any WAL pages.
    replacement = home / 'replacement.db'
    with sqlite3.connect(path) as source, sqlite3.connect(replacement) as target:
        source.backup(target)
        target.execute("UPDATE async_delegations SET delegation_id='replacement'")
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['delegation_id'] == 'old'
    if during_query:
        query = reads._query
        def replacing(*args):
            rows = query(*args)
            replacement.replace(path)
            return rows
        monkeypatch.setattr(reads, '_query', replacing)
        # New scope forces a query while the old handle remains open.
        assert ad.failed_delegations_for_session('ui-1', 'agent-1', now=1001) == []
        monkeypatch.setattr(reads, '_query', query)
    else:
        replacement.replace(path)
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['delegation_id'] == 'replacement'


def test_removed_store_and_schema_unready_recover(home):
    _delegation('old', status='error', result={'status': 'error'}, completed_at=1000)
    assert ad.failed_delegations_for_session('ui-1', now=1001)
    (home / 'state.db').rename(home / 'removed.db')
    assert ad.failed_delegations_for_session('ui-1', now=1001) == []
    with sqlite3.connect(home / 'state.db') as writer:
        writer.execute('CREATE TABLE unrelated (id INTEGER)')
    assert ad.failed_delegations_for_session('ui-1', now=1001) == []
    _delegation('new', status='error', result={'status': 'error'}, completed_at=1000)
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['delegation_id'] == 'new'


def test_commit_during_query_is_not_certified_as_current(home, monkeypatch):
    from tools import async_delegation_focused_reads as reads
    _delegation('old', status='error', result={'status': 'error'}, completed_at=1000)
    query = reads._query
    def racing(*args):
        rows = query(*args)
        with sqlite3.connect(home / 'state.db') as writer:
            writer.execute("UPDATE async_delegations SET result_json=?", ('{"status":"timeout"}',))
        return rows
    monkeypatch.setattr(reads, '_query', racing)
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['status'] == 'error'
    monkeypatch.setattr(reads, '_query', query)
    assert ad.failed_delegations_for_session('ui-1', now=1001)[0]['status'] == 'timeout'


def test_reader_is_readonly_and_scope_cache_is_bounded(home):
    from tools import async_delegation_focused_reads as reads
    _delegation('one', status='error', result={'status': 'error'}, completed_at=1000)
    for i in range(reads._MAX_SCOPES + 1):
        ad.failed_delegations_for_session(f'ui-{i}', now=1001)
    _, conn, scopes = reads._readers.entries[str((home / 'state.db').absolute())]
    assert len(scopes) == reads._MAX_SCOPES
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        conn.execute('DELETE FROM async_delegations')
