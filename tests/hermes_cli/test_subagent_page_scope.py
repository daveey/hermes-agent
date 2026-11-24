"""Page-scoped subagent discovery must not scan unrelated profile inventories."""
import sqlite3

from hermes_cli import subagent_session_activity as activity
from hermes_state import SessionDB


def test_discovery_reads_only_the_page_profiles_and_parents(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = SessionDB(home / 'state.db')
    now = 1700000000
    try:
        def seed(conn):
            conn.executemany("INSERT INTO sessions(id,source,started_at) VALUES (?,'cli',?)",
                             [('parent', now)] + [(f'foreign{i}', now) for i in range(3000)])
            conn.executemany('INSERT INTO sessions(id,source,started_at,parent_session_id) VALUES (?, ?, ?, ?)',
                             [('wanted', 'subagent', now, 'parent')] +
                             [(f'other{i}', 'subagent', now, f'foreign{i}') for i in range(3000)])
        db._execute_write(seed)
        # It is discoverable, but the page does not request it. Opening its store
        # would make an unrelated outage/workload part of this request.
        unrelated = home / 'profiles' / 'other'
        unrelated.mkdir(parents=True)
        (unrelated / 'state.db').touch()
        real = sqlite3.connect
        counts = {'steps': 0, 'queries': 0, 'opens': 0}

        def connect(path, **kwargs):
            assert '/profiles/other/' not in str(path)
            counts['opens'] += 1
            conn = real(path, **kwargs)
            conn.set_progress_handler(lambda: counts.__setitem__('steps', counts['steps'] + 100) or 0, 100)
            conn.set_trace_callback(lambda sql: counts.__setitem__('queries', counts['queries'] + 1))
            return conn

        monkeypatch.setattr(activity.sqlite3, 'connect', connect)
        monkeypatch.setattr(activity.time, 'time', lambda: now + 10)
        monkeypatch.setattr(activity, 'local_scope', lambda: ('edge', 'default'))
        activity._reset_cache_for_tests()
        rows = activity.annotate_subagent_activity([{'id': 'tip', '_lineage_ids': ['parent']}])
        observed = rows[0]['execution_observations']
        assert [o['identity']['generation'] for o in observed] == ['session=wanted']
        assert counts['opens'] == 1
        assert counts['queries'] <= 2
        assert counts['steps'] < 2000, counts
        # A new parent set must not hit the prior page's cached subset.
        rows = activity.annotate_subagent_activity([{'id': 'foreign0'}])
        assert rows[0]['execution_observations'][0]['identity']['generation'] == 'session=other0'
    finally:
        activity._reset_cache_for_tests()
        db.close()
