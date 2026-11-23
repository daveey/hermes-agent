"""Unused discovered board scopes own no capture or watcher. [daveey]"""
import threading
from hermes_cli.fact_board_startup import BoardRuntime
from tests.plugins import test_kanban_board_shadow as helpers
from tests.waiting import wait_until

shadow_board = helpers.shadow_board


def test_eleven_boards_only_activate_the_demanded_scope(shadow_board):
    runtime = BoardRuntime(shadow_board['mod'], boards=('default', *(f'unused-{i}' for i in range(10))))
    try:
        runtime.start()
        assert runtime.owners == {}
        assert runtime.views == {}
        runtime.provider('default')
        assert wait_until(lambda: runtime.ready('default'), timeout=10)
        assert set(runtime.owners) == {'default'}
        assert set(runtime.views) == {'default'}
        assert sum(t.name == 'fact-change-feed' for t in threading.enumerate()) == 1
        assert sum(t.name == 'fact-board-capture' for t in threading.enumerate()) <= 1
        # No source path resolution, watcher or legacy hydration on a request.
        runtime.provider('unknown')
        assert set(runtime.owners) == {'default'}
    finally:
        runtime.close()
