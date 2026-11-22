import shutil
from tests.hermes_cli import test_kanban_origin_comment as f
from tests.hermes_cli import test_kanban_decision_ask as helpers
world = f.world
kanban_home = f.kanban_home
courier = f.courier

def test_omitted_board_honors_db_pin(kanban_home, world, courier, monkeypatch):
    from gateway.session_context import scoped_current_session_id
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_origin_adapter as adapter
    from hermes_cli.kanban_db_boards import create_board
    f.publish(world[1], world[2])
    f.accept(world)
    home = get_hermes_home()
    (home / 'kanban').mkdir(exist_ok=True)
    shutil.copyfile(world[0], home / 'kanban/origin-ledger.db')
    helpers._session('s_old')
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    create_board('build')
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(home / 'kanban/boards/build/kanban.db'))
    with scoped_current_session_id('s_old'):
        explicit = helpers._tool('comment', {'task_id': 't_one', 'body': 'B', 'board': 'build'})
        omitted = helpers._tool('comment', {'task_id': 't_one', 'body': 'B'})
    assert explicit.get('status') == 'comment_pending', explicit
    assert omitted.get('status') == 'comment_pending', omitted
