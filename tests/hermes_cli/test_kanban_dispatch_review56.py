from pathlib import Path
import os
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch import dispatch_once
from tests.hermes_cli import test_kanban_managed_pin_policy as support

board = support.board

@pytest.mark.parametrize('invalid_context', [False, True])
def test_one_profile_cannot_abort_healthy_sibling(board, invalid_context):
    conn, _ = board
    home = Path(os.environ['HERMES_HOME'])
    profile = home / 'profiles' / 'broken'
    profile.mkdir(parents=True)
    config = (home / 'profiles' / 'worker' / 'config.yaml').read_text()
    if invalid_context:
        config = config.replace('  default:', '  context_length: invalid\n  default:')
    (profile / 'config.yaml').write_text(config)
    kb.create_task(conn, title='First profile card', assignee='broken', priority=100)
    peer = kb.create_task(conn, title='Healthy peer', assignee='worker', priority=1)
    spawned = []
    dispatch_once(conn, spawn_fn=lambda task, *a: spawned.append(task.id))
    assert peer in spawned
