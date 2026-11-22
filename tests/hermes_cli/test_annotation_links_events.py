"""Event invalidation must cross inner links caches and close before derivation."""
import sqlite3
import time

import pytest

from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_replica_memo as replica


@pytest.mark.parametrize('display', [False, True])
def test_forced_scope_reprojects_even_with_unchanged_links_key(monkeypatch, display):
    scope = ('event-scope',)
    old = (time.monotonic() + 100, scope, ('unchanged',), [{'stale': False}], [])
    new = (time.monotonic() + 100, scope, ('unchanged',), [{'stale': True}], [])
    monkeypatch.setattr(ksa, '_links_cache', {scope: old})
    monkeypatch.setattr(ksa, '_links_fingerprint', lambda: ('unchanged',))
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', scope, raising=False)
    published = []
    monkeypatch.setattr(ksa, '_publish_links', lambda s, f: published.append((s, f)) or new)
    background = []
    monkeypatch.setattr(ksa, '_refresh_links_in_background', background.append)
    assert ksa._links_lookup(scope, display) is new
    assert published == [(scope, ('unchanged',))]
    assert background == []


def test_force_is_scope_local_and_ordinary_cache_still_reuses(monkeypatch):
    scope = ('ordinary-scope',)
    old = (time.monotonic() + 100, scope, ('unchanged',), [], [])
    monkeypatch.setattr(ksa, '_links_cache', {scope: old})
    monkeypatch.setattr(ksa, '_links_fingerprint', lambda: ('unchanged',))
    monkeypatch.setattr(ksa._revalidate.force_read, 'scope', ('other',), raising=False)
    monkeypatch.setattr(ksa, '_publish_links', lambda *a: pytest.fail('unrelated scope forced'))
    assert ksa._links_lookup(scope, False) is old


@pytest.mark.parametrize('mode', ['incremental', 'full', 'memo'])
def test_replica_finish_runs_after_source_connection_closes(tmp_path, monkeypatch, mode):
    path = tmp_path / 'replica.db'
    writer = sqlite3.connect(path)
    writer.execute('CREATE TABLE replica (coll TEXT, key TEXT, gw TEXT, data TEXT, updated_at INTEGER, seq INTEGER, version INTEGER)')
    writer.commit()
    writer.close()
    opened = []
    original = ksa._open_ro
    def open_one(path):
        conn = original(path)
        opened.append(conn)
        return conn
    monkeypatch.setattr(ksa, '_open_ro', open_one)
    monkeypatch.setattr(ksa, '_self_gateway', lambda conn: 'local')
    monkeypatch.setattr(ksa, '_meta_float', lambda conn, name: None)
    monkeypatch.setattr(ksa, '_remote_read_memo', {})
    original_finish, original_project = replica.finish, ksa._project_replica
    derived, annotated = [], []
    def closed():
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
    def finish(captured, *args, **kwargs):
        assert isinstance(captured, ksa._replica_rows.Rows), 'lazy SQL escaped the snapshot'
        closed()
        derived.append('incremental')
        return original_finish(captured, *args, **kwargs)
    def project(*args, **kwargs):
        closed()
        derived.append('full')
        return original_project(*args, **kwargs)
    def annotate(*args):
        closed()
        annotated.append(True)
    monkeypatch.setattr(replica, 'finish', finish)
    monkeypatch.setattr(ksa, '_project_replica', project)
    monkeypatch.setattr(ksa, '_note_known_gateways', annotate)
    assert ksa._read_remote_links(path, now=time.time() if mode == 'full' else None) == []
    assert derived == ['full' if mode == 'full' else 'incremental']
    assert annotated == [True]
    if mode == 'memo':
        opened.clear()
        derived.clear()
        annotated.clear()
        assert ksa._read_remote_links(path) == []
        assert derived == [], 'unchanged materialized memo must not reproject'
        assert annotated == [True]
