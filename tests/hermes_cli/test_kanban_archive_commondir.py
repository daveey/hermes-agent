import hashlib
import json
import shutil
import subprocess
import tarfile
import pytest

from hermes_cli import kanban_continue_archive as archive
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from tests.hermes_cli import test_kanban_pool_queue as helpers

svc = helpers.svc
w2 = helpers.w2


@pytest.mark.parametrize('original_planner', [True, False])
def test_external_commondir_must_retain_pin(w2, monkeypatch, tmp_path, original_planner):
    from hermes_cli import kanban_park_pin
    if original_planner:
        monkeypatch.setattr(archive, 'plan', kanban_park_pin.plan)
    from pathlib import Path
    from tools import kanban_tools as kt

    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True)

    external, root = tmp_path / 'external', tmp_path / 'workspace'
    git('init', external)
    git('-C', external, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'initial')
    git('-C', external, 'worktree', 'add', '-b', 'work', root)
    metadata = Path((root / '.git').read_text().strip().removeprefix('gitdir: '))
    (root / '.git').unlink()
    shutil.copytree(metadata, root / '.git')
    (root / '.git' / 'commondir').write_text(str(external / '.git') + '\n')
    (root / 'work.txt').write_text('unique unpublished work')
    git('-C', root, 'add', '.')
    git('-C', root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-m', 'unpublished')
    tid = kb.create_task(w2.conn, title='commondir review fixture', assignee='default', runs_on=[helpers.POOL])
    helpers._run(w2, monkeypatch, tid)
    with kb.write_txn(w2.conn):
        w2.conn.execute('UPDATE tasks SET workspace_path = ? WHERE id = ?', (str(root), tid))
    trees, state = kmp.card_trees_state(tid, str(root), '', linked_text_only=True)
    print('REAL PLANNER:', trees, state)
    assert state and str(root) in trees
    target = tmp_path / tid / 'full.tar.gz'
    target.parent.mkdir()
    with tarfile.open(target, 'w:gz') as tar:
        tar.add(root, arcname='.')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    real_run = subprocess.run

    def hub(argv, **kwargs):
        if argv[0] == 'hub-admin':
            return real_run(['bash', '-c', argv[1]], **kwargs)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(archive.subprocess, 'run', hub)
    text = f'portable-workspace: hub-admin:{target} SHA256 {digest}\nUNPUBLISHED_WORK_REASON=review fixture'
    result = json.loads(kt._handle_continue({'checkpoint': text}))
    assert result.get('ok'), result
    assert helpers._runs_on(w2.conn, tid) == ['machine:worker-2']
