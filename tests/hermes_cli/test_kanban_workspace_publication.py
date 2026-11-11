"""Git custody at run handoff; real Git repositories, no credentials/network."""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kw


@pytest.fixture
def git_task(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_TASK',
                'HERMES_KANBAN_RUN_ID', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ['git', '-c', 'user.name=Example', '-c', 'user.email=example@example.com',
             '-C', str(repo), *args], text=True, encoding='utf-8', stderr=subprocess.STDOUT).strip()

    git('init', '-b', 'task/example')
    (repo / 'source.txt').write_text('implementation\n')
    git('add', 'source.txt')
    git('commit', '-m', 'implementation')
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='Git custody fixture')
        kw.set_workspace_path(conn, tid, repo)
        kb.claim_task(conn, tid)
    return tid, repo, git


def test_continue_refuses_unpublished_git_work(git_task):
    tid, _, _ = git_task
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='unpublished_workspace'):
            kc.continue_task(conn, tid, checkpoint='Resume the implementation')
        assert kb.get_task(conn, tid).status == 'running'
        assert kb._current_run_id(conn, tid) is not None


def test_scratch_mention_of_source_repo_is_not_worktree_custody(git_task, tmp_path):
    tid, repo, _ = git_task
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        kw.set_workspace_path(conn, tid, scratch)
        # A source checkout mentioned only for an installed-runtime probe is not
        # an owned implementation worktree. Preserve unrelated host-marker guards.
        assert kc.continue_task(conn, tid, checkpoint=f'Read-only runtime check of {repo}')['ok']


@pytest.mark.parametrize('source', ['checkpoint', 'metadata', 'prior_checkpoint'])
def test_scratch_handoff_cannot_hide_explicit_git_worktree(git_task, tmp_path, source):
    import json

    tid, _, git = git_task
    repo = tmp_path / 'implementation'
    git('worktree', 'add', '-b', 'scratch-implementation', str(repo))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        kw.set_workspace_path(conn, tid, scratch)
        args = {'checkpoint': 'Resume implementation'}
        if source == 'checkpoint':
            args['checkpoint'] = f'Worktree {repo} has implementation; resume it'
        elif source == 'metadata':
            args['metadata'] = {'worktree': str(repo)}
        else:
            with kb.write_txn(conn):
                conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
                             (json.dumps({'checkpoint': f'Worktree {repo} retained'}), kb._current_run_id(conn, tid)))
        with pytest.raises(ValueError, match='unpublished_workspace'):
            kc.continue_task(conn, tid, **args)
        assert kb.get_task(conn, tid).status == 'running'


@pytest.mark.parametrize('operation', ['block', 'schedule', 'review'])
def test_other_handoffs_refuse_unpublished_git_work(git_task, operation):
    tid, _, _ = git_task
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='unpublished_workspace'):
            if operation == 'block':
                kb.block_task(conn, tid, reason='Temporary outage', kind='transient')
            elif operation == 'schedule':
                kb.schedule_task(conn, tid, reason='Waiting for a clock', clock_only=True)
            else:
                kb.request_review(conn, tid, summary='Ready for review')
        assert kb.get_task(conn, tid).status == 'running'


def _publish(git_task, tmp_path):
    _, _, git = git_task
    remote = tmp_path / 'origin.git'
    subprocess.check_call(['git', 'init', '--bare', str(remote)], stdout=subprocess.DEVNULL)
    git('remote', 'add', 'origin', str(remote))
    git('push', 'origin', 'HEAD')
    return remote


def test_published_clean_branch_records_server_receipt(git_task, tmp_path):
    import json

    tid, _, git = git_task
    _publish(git_task, tmp_path)
    with kbc.connect_closing() as conn:
        run = kb._current_run_id(conn, tid)
        result = kc.continue_task(conn, tid, checkpoint='Resume published source',
                                  metadata={'workspace_publication': {'state': 'forged'}})
        assert result['ok']
        receipt = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
        assert receipt['workspace_publication']['state'] == 'published'
        assert receipt['workspace_publication']['head'] == git('rev-parse', 'HEAD')


@pytest.mark.parametrize('change', ['dirty', 'untracked', 'unpushed', 'remote_deleted'])
def test_tracking_ref_is_not_publication_proof(git_task, tmp_path, change):
    tid, repo, git = git_task
    _publish(git_task, tmp_path)
    if change == 'dirty':
        (repo / 'source.txt').write_text('new work')
    elif change == 'untracked':
        (repo / 'new.txt').write_text('new work')
    elif change == 'unpushed':
        (repo / 'source.txt').write_text('new work')
        git('add', 'source.txt')
        git('commit', '-m', 'unpublished')
    else:
        git('push', 'origin', '--delete', 'task/example')
        # Deliberately retain a convincing but stale local tracking ref.
        git('update-ref', 'refs/remotes/origin/task/example', 'HEAD')
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='unpublished_workspace'):
            kc.continue_task(conn, tid, checkpoint='Resume source')
        assert kb.get_task(conn, tid).status == 'running'


def test_explicit_retention_reason_records_host_custody(git_task):
    import json

    tid, _, _ = git_task
    with kbc.connect_closing() as conn:
        run = kb._current_run_id(conn, tid)
        assert kc.continue_task(conn, tid, checkpoint='UNPUBLISHED_WORK_REASON=contains private evidence')['ok']
        receipt = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
        assert receipt['workspace_publication']['state'] == 'unverified'
        assert receipt['workspace_publication']['retention_reason'] == 'contains private evidence'


def test_external_custody_survives_repeated_checkpoint_rollover(git_task, tmp_path):
    tid, _, git = git_task
    repo = tmp_path / 'retained-worktree'
    git('worktree', 'add', '-b', 'retained-implementation', str(repo))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        kw.set_workspace_path(conn, tid, scratch)
        checkpoint = f'Worktree {repo}\nUNPUBLISHED_WORK_REASON=review corrections pending'
        for _ in range(4):
            assert kc.continue_task(conn, tid, checkpoint=checkpoint)['ok']
            assert kb.claim_task(conn, tid)
            checkpoint = 'UNPUBLISHED_WORK_REASON=review corrections still pending'
        with pytest.raises(ValueError, match='unpublished_workspace'):
            kc.continue_task(conn, tid, checkpoint='Resume from prior state')


def test_crash_records_unknown_custody_without_network(git_task, monkeypatch):
    import json
    from hermes_cli import kanban_workspace_publication as pub

    tid, _, _ = git_task
    monkeypatch.setattr(pub, '_observe', lambda _: pytest.fail('crash must not perform network IO'))
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        run = kb._end_run(conn, tid, outcome='crashed', error='fixture')
        receipt = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
        assert receipt['workspace_publication']['state'] == 'unverified'
        assert receipt['workspace_publication']['reason'] == 'involuntary_or_unguarded_exit'


def test_local_mutation_after_probe_rolls_back_handoff(git_task, tmp_path, monkeypatch):
    from hermes_cli import kanban_workspace_publication as pub

    tid, repo, _ = git_task
    _publish(git_task, tmp_path)
    observe = pub._observe

    def racing_observe(path):
        result = observe(path)
        (repo / 'source.txt').write_text('racing work')
        return result

    monkeypatch.setattr(pub, '_observe', racing_observe)
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='workspace changed before handoff'):
            kc.continue_task(conn, tid, checkpoint='Resume source')
        assert kb.get_task(conn, tid).status == 'running'
        assert kb._current_run_id(conn, tid) is not None
