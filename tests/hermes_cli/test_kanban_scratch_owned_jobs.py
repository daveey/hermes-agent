"""Completion/GC must not remove the cwd of a live, explicitly owned job."""
from contextlib import contextmanager
import socket
import subprocess
import sys

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_git_preservation as helpers
from tests.waiting import wait_until
from tools.environments.local import build_subprocess_env

board = helpers.board


@contextmanager
def writer(path, task_id, monkeypatch):
    script = (
        "from pathlib import Path; import sys; "
        "Path('ready').write_text('ready'); sys.stdin.readline(); "
        "Path('after-completion').write_text('still writing')"
    )
    env = build_subprocess_env()
    env['HERMES_OWNER_CARD'] = f'{task_id}@{socket.gethostname()}'
    proc = subprocess.Popen(
        [sys.executable, '-c', script], cwd=path, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
    )
    try:
        wait_until(lambda: (path / 'ready').exists(), timeout=10)
        # Keep discovery isolated; all identity/cwd/environment reads are real.
        monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: iter([psutil.Process(proc.pid)]))
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=10)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('entry', ['complete', 'parent', 'gc', 'archive'])
def test_owned_writer_survives_cleanup_then_gc_reclaims(board, monkeypatch, entry):
    tid, path = helpers.scratch(board)
    child = None
    if entry == 'parent':
        child = kb.create_task(board, title='child', workspace_kind='dir', workspace_path=str(path / 'child'))
        kb.link_tasks(board, tid, child)
    with writer(path, tid, monkeypatch) as proc:
        if entry == 'archive':
            # Exercise the destructive post-archive recheck independently of the first GC guard.
            from hermes_cli import kanban_scratch_archive as archive
            monkeypatch.setattr(archive, 'archive_workspace', lambda *a, **kw: path.parent / 'saved')
            with kb.write_txn(board):
                board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            assert gc._archive_and_remove_scratch(board, tid, path, None) == 0
        elif entry == 'gc':
            with kb.write_txn(board):
                board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            assert gc.gc_task_scratch(board) == 0
        else:
            assert kb.complete_task(board, tid, result='done')
            if child:
                assert kb.complete_task(board, child, result='done')
        assert path.is_dir(), 'cleanup removed a live owned writer cwd'
        stdout, stderr = proc.communicate('write\n', timeout=10)
        assert proc.returncode == 0, (stdout, stderr)
        assert (path / 'after-completion').read_text() == 'still writing'
        monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: iter([]))
        assert gc.gc_task_scratch(board) == 1
        assert not path.exists()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('relation', ['sharer', 'child'])
def test_other_card_owned_writer_keeps_shared_scratch(board, monkeypatch, relation):
    tid, path = helpers.scratch(board)
    other = kb.create_task(board, title='other owner', workspace_kind='dir', workspace_path=str(path))
    if relation == 'child':
        kb.link_tasks(board, tid, other)
    with writer(path, other, monkeypatch) as proc:
        assert kb.complete_task(board, other if relation == 'sharer' else tid, result='done')
        assert kb.complete_task(board, tid if relation == 'sharer' else other, result='done')
        assert path.is_dir(), 'the remaining writer belongs to the other card'
        stdout, stderr = proc.communicate('write\n', timeout=10)
        assert proc.returncode == 0, (stdout, stderr)
        assert (path / 'after-completion').read_text() == 'still writing'


def test_inactive_owner_note_does_not_pin_workspace(board, monkeypatch):
    tid, path = helpers.scratch(board)
    (path / 'OWNER.md').write_text(f'card: {tid}@{socket.gethostname()}\npid: 12345\n')
    monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: iter([]))
    assert kb.complete_task(board, tid, result='done')
    assert not path.exists()
