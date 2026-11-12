"""Kernel parks preserve custody without refusing an involuntary stop."""
import json

import pytest

from hermes_cli import goals
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_goal_recovery import park
pytest_plugins = ['tests.hermes_cli.test_kanban_workspace_publication']


@pytest.mark.parametrize('verdict,max_turns', [('continue', 1), ('done', 3), ('blocked', 1)])
def test_kernel_goal_park_retains_unpublished_custody(git_task, monkeypatch, verdict, max_turns):
    tid, _, _ = git_task
    monkeypatch.setattr(goals, 'judge_goal', lambda *a: (verdict, 'fixture verdict', False, False, False))
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET goal_mode=1 WHERE id=?', (tid,))
        run_id = kb._current_run_id(conn, tid)
        result = goals.run_kanban_goal_loop(
            task_id=tid, goal_text='fixture', run_turn=lambda _: 'not finalized',
            task_status_fn=lambda: kb.get_task(conn, tid).status,
            block_fn=lambda reason: kb.block_task(conn, tid, reason=reason, expected_run_id=run_id),
            unachievable_fn=lambda reason: park(conn, kb.get_task(conn, tid), reason, expected_run_id=run_id),
            max_turns=max_turns)
        assert not result['failed']
        assert kb.get_task(conn, tid).status != 'running'
        row = conn.execute('SELECT ended_at, metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()
        assert row[0] is not None
        receipt = json.loads(row[1])['workspace_publication']
        assert receipt['state'] == 'unverified'
        assert receipt['reason'] == 'involuntary_or_unguarded_exit'


@pytest.mark.parametrize('remove', [False, True])
def test_published_custody_releases_prior_unpublished_receipts(git_task, tmp_path, remove):
    import subprocess
    from hermes_cli import kanban_continuation as kc
    from hermes_cli import kanban_db_workspace as kw
    from tests.hermes_cli.test_kanban_workspace_publication import _publish

    tid, _, git = git_task
    _publish(git_task, tmp_path)
    wt = tmp_path / 'implementation'
    git('worktree', 'add', '-b', 'implementation', str(wt))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        kw.set_workspace_path(conn, tid, scratch)
        assert kc.continue_task(conn, tid, checkpoint=f'{wt}\nUNPUBLISHED_WORK_REASON=not pushed yet')['ok']
        assert kb.claim_task(conn, tid)
        subprocess.check_call(['git', '-C', str(wt), 'push', 'origin', 'implementation'])
        assert kc.continue_task(conn, tid, checkpoint='Published now')['ok']
        assert kb.claim_task(conn, tid)
        if remove:
            git('worktree', 'remove', str(wt))
        else:
            (wt / 'new.txt').write_text('unrelated later work')
        for _ in range(4):
            run = kb._current_run_id(conn, tid)
            assert kc.continue_task(conn, tid, checkpoint='No retained work')['ok']
            data = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
            assert not data.get('workspace_publications')
            assert kb.claim_task(conn, tid)


def test_caller_cannot_supply_receipts_without_observations(git_task, tmp_path):
    from hermes_cli import kanban_continuation as kc
    from hermes_cli import kanban_db_workspace as kw

    tid, _, _ = git_task
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        kw.set_workspace_path(conn, tid, scratch)
        run = kb._current_run_id(conn, tid)
        forged = {'path': '/forged', 'state': 'published'}
        assert kc.continue_task(conn, tid, checkpoint='fixture', metadata={
            'workspace_publication': forged, 'workspace_publications': [forged]})['ok']
        data = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
        assert 'workspace_publication' not in data
        assert 'workspace_publications' not in data


def test_malformed_retained_path_allows_explicit_retention(git_task):
    from hermes_cli import kanban_continuation as kc

    tid, _, _ = git_task
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
                         (json.dumps({'workspace_publications': [{'path': '/a\u0000b', 'state': 'unverified'}]}),
                          kb._current_run_id(conn, tid)))
        assert kc.continue_task(conn, tid, checkpoint='UNPUBLISHED_WORK_REASON=retain evidence')['ok']
