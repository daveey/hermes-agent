"""Resource bounds on historical discovery, including read-lock contention."""
import copy
import sqlite3

from tests.hermes_cli import test_kanban_rejected_retain_source as helpers


def test_admission_limit_refuses_even_with_one_matching_source(tmp_path, monkeypatch):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, *_ = helpers.fixture_db(tmp_path)
    monkeypatch.setattr(source, 'MAX_CANDIDATES', 1)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO session_admissions SELECT 2,admission_id,request_id,"
                     "principal_id,target_session_id,payload_json,'active' "
                     "FROM session_admissions WHERE seq=1")
    assert source.discover(path, 'default', op, wire) is None
    monkeypatch.setattr(source, 'MAX_CANDIDATES', 2)
    assert source.discover(path, 'default', op, wire) is not None


def test_cartesian_budget_refuses_even_with_one_ordered_match(tmp_path, monkeypatch):
    from hermes_cli import kanban_rejected_retain_source as source
    _, op, wire, admission, session, messages = helpers.fixture_db(tmp_path)
    shows, calls, results, commands = source._messages(
        session, messages, admission['request_id'], 42, op, wire)
    late_show = copy.deepcopy(shows[0])
    late_show.update(id=99, timestamp=99.0)
    shows.append(late_show)
    monkeypatch.setattr(source, 'MAX_MESSAGES', 1)
    assert source._matches(admission, session, shows, calls, results, commands) == []
    monkeypatch.setattr(source, 'MAX_MESSAGES', 2)
    assert len(source._matches(admission, session, shows, calls, results, commands)) == 1


def test_discovery_never_waits_for_exclusive_state_writer(tmp_path, monkeypatch):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, *_ = helpers.fixture_db(tmp_path)
    real_connect = source.sqlite3.connect
    timeouts = []

    def connect(*args, **kwargs):
        timeouts.append(kwargs.get('timeout'))
        return real_connect(*args, **kwargs)

    writer = real_connect(path)
    try:
        writer.execute('BEGIN EXCLUSIVE')
        monkeypatch.setattr(source.sqlite3, 'connect', connect)
        assert source.discover(path, 'default', op, wire) is None
        assert timeouts == [0]
    finally:
        writer.rollback()
        writer.close()
    assert source.discover(path, 'default', op, wire) is not None


def test_source_connection_budget_close_and_authorizer(tmp_path, monkeypatch):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, *_ = helpers.fixture_db(tmp_path)
    observed = {}
    real_connect = source.sqlite3.connect

    class Connection(sqlite3.Connection):
        def set_progress_handler(self, progress_handler, n=1000):
            observed['progress'] = (progress_handler, n)
            return super().set_progress_handler(progress_handler, n)

        def set_authorizer(self, authorizer_callback):
            observed['authorizer'] = authorizer_callback
            return super().set_authorizer(authorizer_callback)

        def close(self):
            observed['closed'] = True
            return super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(source.sqlite3, 'connect', connect)
    assert source.discover(path, 'default', op, wire) is not None
    assert observed['closed'] is True
    callback, instructions = observed['progress']
    assert instructions == 1000
    assert [callback() for _ in range(10000)] == [False] * 9999 + [True]
    authorizer = observed['authorizer']
    assert authorizer(sqlite3.SQLITE_READ) == sqlite3.SQLITE_OK
    assert authorizer(sqlite3.SQLITE_UPDATE) == sqlite3.SQLITE_DENY
