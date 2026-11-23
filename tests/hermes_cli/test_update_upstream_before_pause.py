"""The upstream fetch, compare and fork merge run BEFORE the gateways are paused (worker-6, 111 s).

``hermes update`` on a fork used to fetch upstream (77 s on a partial clone), build the off-tree
fork merge and file its conflict card while this install's gateways were stopped. None of that
writes a file of the live checkout. A call-order recorder over a real ``cmd_update`` on local git
fixtures (no network) shows that it now runs before the pause, while the fast-forward from upstream,
a real tree move, still runs paused and under ``checkout_move``.
"""
from contextlib import contextmanager
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main as cli_main, update_cmd, update_cmd_git, update_receipt
from hermes_cli.update_inventory import UpdatePlan


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding='utf-8').stdout.strip()


def commit(root, text, message):
    (root / 'content.txt').write_text(text, encoding='utf-8')
    git(root, '-c', 'commit.gpgsign=false', 'commit', '-qam', message)
    return git(root, 'rev-parse', 'HEAD')


class Recorder:
    """What ran, in order, and whether this run's gateways were paused (or a tree move open) then."""

    def __init__(self):
        self.events, self.paused, self.moves = [], False, []

    def note(self, name, *detail):
        self.events.append((name, *detail, 'paused' if self.paused else 'running',
                            self.moves[-1] if self.moves else None))

    def names(self, state=None):
        return [e[0] for e in self.events if e[0] != 'pause' and (state is None or e[-2] == state)]


@pytest.fixture
def fork(tmp_path, monkeypatch):
    for key in ('HERMES_UPDATE_HANDOFF_PID', 'HERMES_UPDATE_REEXEC'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'git-config'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    origin = tmp_path / 'origin'
    origin.mkdir()
    git(origin, 'init', '-q', '-b', 'main')
    git(origin, 'config', 'user.name', 'Fixture')
    git(origin, 'config', 'user.email', 'fixture@example.invalid')
    (origin / 'content.txt').write_text('base\n', encoding='utf-8')
    (origin / '.gitignore').write_text('.bytecode-fingerprint\n', encoding='utf-8')
    git(origin, 'add', 'content.txt', '.gitignore')
    git(origin, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'base')
    base = git(origin, 'rev-parse', 'HEAD')
    clone = tmp_path / 'install'
    git(tmp_path, 'clone', '-q', str(origin), str(clone))
    upstream = tmp_path / 'upstream'
    git(tmp_path, 'clone', '-q', str(origin), str(upstream))
    for repo in (clone, upstream):
        git(repo, 'config', 'user.name', 'Fixture')
        git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(clone, 'remote', 'add', 'upstream', str(upstream))

    monkeypatch.setattr(cli_main, 'PROJECT_ROOT', clone)
    monkeypatch.setattr(update_receipt, '_code_identity', lambda **_: {'commit': base})
    monkeypatch.setattr(cli_main, '_run_pre_update_backup', lambda *_: 'snapshot')
    monkeypatch.setattr(cli_main, '_pause_windows_gateways_for_update', lambda: None)
    from hermes_cli import update_cmd_pause_gate, update_cmd_posix_pause as posix_pause, update_custody
    monkeypatch.setattr(posix_pause, '_discover_systemd', list)
    monkeypatch.setattr(posix_pause, '_discover_launchd', list)
    monkeypatch.setattr(posix_pause, '_discover_bare', lambda pids: ([], []))
    monkeypatch.setattr(cli_main, '_resume_windows_gateways_after_update', lambda state: None)
    monkeypatch.setattr(cli_main, '_install_hangup_protection', lambda **_: {'installed': False})
    monkeypatch.setattr(cli_main, '_finalize_update_output', lambda *_: None)
    monkeypatch.setattr('hermes_cli.update_inventory.collect_runtime_inventory', lambda: UpdatePlan(
        install_method='git', expected_sha=git(clone, 'rev-parse', 'HEAD'), profiles=['default']))
    monkeypatch.setattr(cli_main, '_sync_with_upstream_if_needed', update_cmd_git._sync_with_upstream_if_needed)
    requests = []
    monkeypatch.setattr(update_cmd, 'run_completion', lambda request: requests.append(request) or {
        'exit_code': 0, 'receipt': None})

    rec = Recorder()
    real_pause = posix_pause.pause_at_commit_point

    def pause():  # the commit point stops this install's gateways; the update resumes them at its end
        if not rec.paused:
            rec.note('pause')
        rec.paused = True
        return real_pause()

    monkeypatch.setattr(posix_pause, 'pause_at_commit_point', pause)
    real_run_git = update_custody.run_git

    def run_git(git_cmd, args, *a, **kw):
        if args[:2] == ['fetch', 'upstream']:
            rec.note('fetch upstream')
        if args[:2] == ['merge', '--ff-only']:
            rec.note('merge --ff-only', args[2])
        return real_run_git(git_cmd, args, *a, **kw)

    monkeypatch.setattr(update_custody, 'run_git', run_git)
    real_move = update_cmd_pause_gate._checkout_move

    @contextmanager
    def checkout_move(token, *targets, **kw):
        with real_move(token, *targets, **kw) as step:
            rec.moves.append(targets)
            try:
                yield step
            finally:
                rec.moves.pop()

    monkeypatch.setattr(update_cmd_pause_gate, '_checkout_move', checkout_move)
    report = []
    from hermes_cli.update_upstream_conflict import report as real_report

    def conflict_report(cwd, sha, conflicts):
        rec.note('conflict report')
        report.append((sha, conflicts))
        return 't_conflict_owner'

    monkeypatch.setattr('hermes_cli.update_upstream_conflict.report', conflict_report)
    gate = []

    def ci_gate(git_cmd, cwd, head='HEAD'):
        rec.note('ci gate')
        gate.append(head)
        return False

    monkeypatch.setattr(update_cmd_git, '_sync_fork_with_upstream', ci_gate)
    args = SimpleNamespace(branch=None, channel='main', yes=True, force=True, force_venv=True, check=False,
                           plan=False, gateway=True, install_id=False, set_channel=None)
    return SimpleNamespace(origin=origin, clone=clone, upstream=upstream, base=base, rec=rec, args=args,
                           requests=requests, report=report, gate=gate, real_report=real_report)


@pytest.mark.parametrize('conflict', [True, False], ids=['conflict', 'clean'])
def test_fork_merge_fetches_merges_and_reports_before_the_gateways_pause(fork, monkeypatch, conflict):
    t = fork
    monkeypatch.setattr(update_cmd_git, '_fork_merges_upstream', lambda: True)
    upstream_tip = commit(t.upstream, 'upstream\n', 'upstream change')
    if not conflict:
        (t.origin / 'fork.txt').write_text('fork\n', encoding='utf-8')
        git(t.origin, 'add', 'fork.txt')
        git(t.origin, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fork change')
        fork_tip = git(t.origin, 'rev-parse', 'HEAD')
    else:
        fork_tip = commit(t.origin, 'fork\n', 'fork change')

    cli_main.cmd_update(t.args)

    # The origin pull still moved the tree (paused); the fork merge never did.
    assert git(t.clone, 'rev-parse', 'HEAD') == fork_tip
    assert len(t.requests) == 1
    off_tree = ['fetch upstream', 'conflict report'] if conflict else ['fetch upstream', 'ci gate']
    assert t.rec.names('running') == off_tree, t.rec.events
    assert t.rec.names('paused') == ['merge --ff-only'], t.rec.events  # the origin fast-forward only
    assert [e[0] for e in t.rec.events].index('pause') == len(off_tree)
    if conflict:
        assert t.report == [(upstream_tip, ['content.txt'])] and t.gate == []
    else:
        [merged] = t.gate  # built on origin/main, where the pull put HEAD, before the pause
        assert git(t.clone, 'rev-list', '--parents', '-n1', merged).split()[1:] == [fork_tip, upstream_tip]
        assert t.report == []


def test_worker_conflict_keeps_fork_update_without_local_owner(fork, tmp_path, monkeypatch, capsys):
    from tests.hermes_cli.test_update_upstream_conflict import _gateway_identity
    from hermes_cli.config import save_config
    from hermes_cli.kanban_db_connect import connect_closing

    _gateway_identity(tmp_path, monkeypatch, 'worker-a4')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    save_config({'platform_toolsets': {'cli': ['terminal', 'file']}})
    monkeypatch.setattr('hermes_cli.update_upstream_conflict.report', fork.real_report)
    monkeypatch.setattr(update_cmd_git, '_fork_merges_upstream', lambda: True)
    commit(fork.upstream, 'upstream\n', 'upstream change')
    fork_tip = commit(fork.origin, 'fork\n', 'fork change')

    cli_main.cmd_update(fork.args)

    assert git(fork.clone, 'rev-parse', 'HEAD') == fork_tip
    assert len(fork.requests) == 1 and fork.gate == []
    assert 'upstream integration belongs to the hub' in capsys.readouterr().out
    with connect_closing() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_upstream_fast_forward_still_moves_the_tree_paused_under_checkout_move(fork):
    t = fork
    origin_tip = commit(t.origin, 'origin\n', 'origin change')
    git(t.upstream, 'pull', '-q', '--ff-only', 'origin', 'main')
    upstream_tip = commit(t.upstream, 'upstream\n', 'upstream change')  # the fork is strictly behind

    cli_main.cmd_update(t.args)

    assert git(t.clone, 'rev-parse', 'HEAD') == upstream_tip
    assert t.rec.names('running') == ['fetch upstream'], t.rec.events
    ff = [e for e in t.rec.events if e[0] == 'merge --ff-only']
    assert [e[1] for e in ff] == [origin_tip, upstream_tip]
    upstream_ff = ff[1]
    # Gateways stopped, inside a checkout_move whose tree gate is bound to the upstream commit.
    assert upstream_ff[-2:] == ('paused', (upstream_tip,)), t.rec.events
    # Unchanged here: the validated sync is published through the gate after the syntax guard.
    assert t.gate == ['HEAD'] and t.report == []


def test_prepared_plan_is_used_once_and_never_fetches_again(fork, monkeypatch):
    """The paused sync reuses the prepared fetch and merge; a second sync plans afresh."""
    t = fork
    monkeypatch.setattr(update_cmd_git, '_fork_merges_upstream', lambda: True)
    commit(t.upstream, 'upstream\n', 'upstream change')
    commit(t.clone, 'fork\n', 'fork change')
    git(t.clone, 'update-ref', 'refs/remotes/origin/main', git(t.clone, 'rev-parse', 'HEAD'))
    update_cmd_git._prepare_upstream_sync(['git'], t.clone, enabled=True, assume_yes=True)
    assert t.rec.names() == ['fetch upstream', 'conflict report']
    assert update_cmd_git._sync_with_upstream_if_needed(['git'], t.clone, assume_yes=True) is False
    assert t.rec.names() == ['fetch upstream', 'conflict report']  # nothing ran twice
    assert update_cmd_git._sync_with_upstream_if_needed(['git'], t.clone, assume_yes=True) is False
    assert t.rec.names() == ['fetch upstream', 'conflict report'] * 2


def test_prepared_merge_is_rebuilt_on_head_when_head_is_elsewhere(fork, monkeypatch):
    t = fork
    monkeypatch.setattr(update_cmd_git, '_fork_merges_upstream', lambda: True)
    upstream_tip = commit(t.upstream, 'upstream\n', 'upstream change')
    (t.origin / 'fork.txt').write_text('fork\n', encoding='utf-8')
    git(t.origin, 'add', 'fork.txt')
    git(t.origin, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fork change')
    git(t.clone, 'fetch', '-q', 'origin')
    origin_tip = git(t.clone, 'rev-parse', 'origin/main')
    update_cmd_git._prepare_upstream_sync(['git'], t.clone, enabled=True, assume_yes=True)
    assert len(t.gate) == 1  # built on origin/main before any pause
    # HEAD did not land on origin/main (here: never pulled), so the merge is not the one prepared.
    assert update_cmd_git._sync_with_upstream_if_needed(['git'], t.clone, assume_yes=True) is True
    assert t.rec.names() == ['fetch upstream', 'ci gate', 'ci gate']
    first, second = (git(t.clone, 'rev-list', '--parents', '-n1', m).split()[1:] for m in t.gate)
    assert first == [origin_tip, upstream_tip] and second == [t.base, upstream_tip]


def test_no_merge_before_the_pause_when_the_pull_cannot_land_on_origin_main(fork, monkeypatch):
    """A HEAD with commits origin/main lacks does not land on origin/main: build the merge on HEAD later."""
    t = fork
    monkeypatch.setattr(update_cmd_git, '_fork_merges_upstream', lambda: True)
    upstream_tip = commit(t.upstream, 'upstream\n', 'upstream change')
    (t.clone / 'local.txt').write_text('local\n', encoding='utf-8')
    git(t.clone, 'add', 'local.txt')
    git(t.clone, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'local')
    local = git(t.clone, 'rev-parse', 'HEAD')
    (t.origin / 'fork.txt').write_text('fork\n', encoding='utf-8')
    git(t.origin, 'add', 'fork.txt')
    git(t.origin, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fork change')
    git(t.clone, 'fetch', '-q', 'origin')
    update_cmd_git._prepare_upstream_sync(['git'], t.clone, enabled=True, assume_yes=True)
    assert t.rec.names() == ['fetch upstream'] and t.gate == []
    assert update_cmd_git._sync_with_upstream_if_needed(['git'], t.clone, assume_yes=True) is True
    assert t.rec.names() == ['fetch upstream', 'ci gate']  # no second fetch
    [merged] = t.gate
    assert git(t.clone, 'rev-list', '--parents', '-n1', merged).split()[1:] == [local, upstream_tip]


def test_disabled_or_foreign_root_prepares_nothing(fork, tmp_path):
    t = fork
    commit(t.upstream, 'upstream\n', 'upstream change')
    update_cmd_git._prepare_upstream_sync(['git'], t.clone, enabled=True, assume_yes=True)
    assert update_cmd_git._PREPARED is not None and t.rec.names() == ['fetch upstream']
    # A later attempt that does not sync upstream drops the earlier attempt's plan and fetches nothing.
    update_cmd_git._prepare_upstream_sync(['git'], t.clone, enabled=False, assume_yes=True)
    assert update_cmd_git._PREPARED is None and t.rec.names() == ['fetch upstream']
    update_cmd_git._prepare_upstream_sync(['git'], t.clone, enabled=True, assume_yes=True)
    assert update_cmd_git._take_prepared_upstream_sync(Path(tmp_path / 'elsewhere')) is None
    assert update_cmd_git._PREPARED is None  # handed out (or dropped) once
