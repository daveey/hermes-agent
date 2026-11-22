import shutil
import sqlite3

from tests.hermes_cli import test_kanban_origin_comment as fixtures
from tests.hermes_cli import test_kanban_decision_ask as helpers
from hermes_cli import kanban_origin_comment as comments

world = fixtures.world
kanban_home = fixtures.kanban_home
courier = fixtures.courier
publish = fixtures.publish
accept = fixtures.accept


def test_retry_after_real_compression_reuses_request(kanban_home, world, courier, monkeypatch):
    from gateway.session_context import scoped_current_session_id
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_origin_adapter as adapter
    from hermes_cli import kanban_db_connect as kbc
    from hermes_state import SessionDB

    binding = world[2]
    binding['owner']['board'] = 'default'
    binding['task_record_key'] = 'work/default/t_one'
    publish(world[1], binding)
    accept(world)
    home = get_hermes_home()
    (home / 'kanban').mkdir(exist_ok=True)
    shutil.copyfile(world[0], home / 'kanban/origin-ledger.db')
    helpers._session('s_old')
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with kbc.connect_closing(home / 'kanban' / 'probe.db') as conn:
        with scoped_current_session_id('s_old'):
            first = comments.forward(conn, 't_one', {}, 'Choose B')
        # Real continuation rows, consumed by the production lineage resolver.
        db = SessionDB(home / 'state.db')
        db.end_session('s_old', 'compression')
        db.create_session('s_new', source='desktop', parent_session_id='s_old')
        db.close()
        assert adapter._resolve_session(home / 'state.db', 's_old') == 's_new'
        with scoped_current_session_id('s_new'):
            second = comments.forward(conn, 't_one', {}, 'Choose B')
    assert first['request_id'] == second['request_id'], (first, second)
    assert len(courier[1]) == 2
    assert courier[1][0][0] == courier[1][1][0]
    assert [call[1]['env']['HERMES_SESSION_ID'] for call in courier[1]] == ['s_old', 's_old']


def test_omitted_board_uses_active_board(kanban_home, world, courier, monkeypatch):
    from gateway.session_context import scoped_current_session_id
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_origin_adapter as adapter
    publish(world[1], world[2])
    accept(world)
    home = get_hermes_home()
    (home / 'kanban').mkdir(exist_ok=True)
    shutil.copyfile(world[0], home / 'kanban/origin-ledger.db')
    helpers._session('s_old')
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    from hermes_cli.kanban_db_boards import create_board
    create_board('build')
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'build')
    with scoped_current_session_id('s_old'):
        explicit = helpers._tool('comment', {'task_id': 't_one', 'body': 'B', 'board': 'build'})
        omitted = helpers._tool('comment', {'task_id': 't_one', 'body': 'B'})
    assert explicit.get('status') == 'comment_pending', explicit
    assert omitted.get('status') == 'comment_pending', omitted
