"""Session bootstrap retains both listing and content windows. [daveey]"""
import pytest

from hermes_cli.fact_capture import CaptureBudget, CapturePending, CaptureRefused
from hermes_cli.fact_session_source import SessionContentSource
from tests.hermes_cli import test_fact_session_source as helpers
from tests.hermes_cli.test_fact_task_bootstrap import finish

store = helpers.store


def test_session_pages_replay_both_clocks_and_late_rekeys(store):
    db, reader, path = store
    for i in range(12):
        db.create_session(f's{i:02}', 'cli')
    seen = []
    def hydrate(conn, ids):
        seen.append(ids)
        return helpers.rows(conn, ids)
    source = SessionContentSource('sessions', path, hydrate)
    budget = CaptureBudget(max_keys=3, max_seconds=2)
    for _ in range(2):
        with pytest.raises(CapturePending):
            source.capture(None, budget)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET title='changed-' || id,input_tokens=17"))
    while not source._bootstrap.replay:
        with pytest.raises(CapturePending):
            source.capture(None, budget)
    with pytest.raises(CapturePending):
        source.capture(None, budget)
    def late(c):
        c.execute("UPDATE sessions SET input_tokens=29")
        c.execute("UPDATE sessions SET id='renamed' WHERE id='s00'")
        c.execute("DELETE FROM sessions WHERE id='s01'")
    db._execute_write(late)
    result = finish(source, budget)
    actual = {row['id']: (row['title'], row['input_tokens']) for row in result.records}
    expected = {row[0]: (row[1], row[2]) for row in reader.execute('SELECT id,title,input_tokens FROM sessions')}
    assert actual == expected and result.full and max(map(len, seen)) <= 3
    assert source.capture(None, budget) is result
    assert not source.capture(result.after, budget).ids


def test_large_session_bootstrap_under_default_capture_budget(store):
    db, reader, path = store
    db._execute_write(lambda c: c.executemany(
        "INSERT INTO sessions(id,source,started_at) VALUES (?,'cli',1)",
        ((f'large-{i:05}',) for i in range(27000))))
    batches = []
    def hydrate(conn, ids):
        batches.append(len(ids))
        return helpers.rows(conn, ids)
    source = SessionContentSource('sessions', path, hydrate)
    for _ in range(300):
        try:
            result = source.capture(None, CaptureBudget())
            break
        except CapturePending:
            continue
    else:
        pytest.fail('large session bootstrap did not converge')
    assert len(result.records) == reader.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 27000
    assert max(batches) <= 500
    assert source.capture(None, CaptureBudget()) is result


def test_session_epoch_change_discards_staging(store):
    db, _, path = store
    for i in range(4):
        db.create_session(f's{i}', 'cli')
    source = SessionContentSource('sessions', path, helpers.rows)
    budget = CaptureBudget(max_keys=2, max_seconds=2)
    for _ in range(2):
        with pytest.raises(CapturePending):
            source.capture(None, budget)
    db._execute_write(lambda c: c.execute("UPDATE session_content_revision SET epoch='restored'"))
    with pytest.raises(CaptureRefused, match='coverage'):
        source.capture(None, budget)
    assert source._bootstrap.rows == {}
    assert len(finish(source, budget).records) == 4
