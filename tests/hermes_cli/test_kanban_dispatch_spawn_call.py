"""The dispatcher keeps its compatibility export after helper extraction."""
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_dispatch_spawn_call import call_spawn_fn as _call_spawn_fn


def test_dispatcher_export_is_same_helper():
    assert dispatch._call_spawn_fn is _call_spawn_fn


def test_legacy_callable_gets_two_arguments():
    calls = []
    def spawn(task, workspace):
        calls.append((task, workspace))
        return 123
    assert _call_spawn_fn(spawn, "task", "workspace", "board") == 123
    assert calls == [("task", "workspace")]


def test_board_aware_callable_retains_board():
    calls = []
    def spawn(task, workspace, board=None):
        calls.append((task, workspace, board))
        return 456
    assert _call_spawn_fn(spawn, "task", "workspace", "board") == 456
    assert calls == [("task", "workspace", "board")]
