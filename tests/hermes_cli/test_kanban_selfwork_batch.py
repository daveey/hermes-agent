"""Daily self-work ownership; product creation remains ordinary creation."""
from datetime import UTC, datetime

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as db:
        yield db


def declaration(area='kanban', kind='sweep'):
    return dict(version=1, repository='hermes-core', area=area, kind=kind)


def admit(conn, request, value, now):
    from hermes_cli.kanban_selfwork_intake import admit as intake
    return intake(conn, request_id=request, authority='hub', local_gateway='hub', now=now,
                  options=dict(title=request, body='Evidence for ' + request, self_work=value))


def test_second_same_area_appends(conn):
    first = admit(conn, 'request-one', declaration(), 100000)
    second = admit(conn, 'request-two', declaration(kind='test-gap'), 100001)
    assert second['task_id'] == first['task_id']
    assert second['created'] is False
    comments = conn.execute('SELECT body FROM task_comments WHERE task_id=?', (first['task_id'],)).fetchall()
    assert len(comments) == 1
    assert 'Evidence for request-two' in comments[0][0]
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('other,now', [(declaration('runtime'), 100001), (declaration(), 186400)])
def test_different_area_or_day_creates(conn, other, now):
    first = admit(conn, 'request-one', declaration(), 100000)
    second = admit(conn, 'request-two', other, now)
    assert second['task_id'] != first['task_id']
    assert second['created'] is True


@pytest.mark.parametrize('title', ['BB guard fix', 'Ask Eve post-merge', 'fabric test-gap'])
def test_product_cards_never_batched(conn, title):
    first = kb.create_task(conn, title=title)
    second = kb.create_task(conn, title=title)
    assert first != second


def test_midnight_boundary(conn):
    midnight = int(datetime(2026, 11, 15, tzinfo=UTC).timestamp())
    first = admit(conn, 'request-one', declaration(), midnight - 1)
    second = admit(conn, 'request-two', declaration(), midnight)
    assert first['task_id'] != second['task_id']


def test_retry_does_not_append_twice(conn):
    admit(conn, 'request-one', declaration(), 100000)
    second = admit(conn, 'request-two', declaration(), 100001)
    retry = admit(conn, 'request-two', declaration(), 100002)
    assert retry['replayed'] is True
    assert retry['task_id'] == second['task_id']
    assert conn.execute('SELECT count(*) FROM task_comments').fetchone()[0] == 1
