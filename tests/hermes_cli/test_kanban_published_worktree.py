"""Published clean worktrees are reconstructible, not host-only state."""
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_placement as kp


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    origin = tmp_path / 'origin.git'
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'commit', '--allow-empty', '-m', 'base')
    git(repo, 'remote', 'add', 'origin', 'https://github.com/example/project.git')
    git(repo, 'config', f'url.{origin}.insteadOf', 'https://github.com/example/project.git')
    wt = tmp_path / 'code' / '_wt' / 'fix'
    git(repo, 'worktree', 'add', '-b', 'wt/fix', str(wt))
    git(wt, 'push', 'origin', 'HEAD:refs/heads/wt/fix')
    git(repo, 'config', '--unset', f'url.{origin}.insteadOf')
    # Real local Git state and remote protocol; only substitute transport target.
    check_output = subprocess.check_output

    def transport(argv, **kw):
        if argv[-4:] == ['ls-remote', '--exit-code', 'origin', 'refs/heads/wt/fix']:
            argv = [*argv[:-2], str(origin), argv[-1]]
        return check_output(argv, **kw)

    monkeypatch.setattr(subprocess, 'check_output', transport)
    return wt, origin


def reason(body, **extra):
    head = git(Path.home() / 'code' / '_wt' / 'fix', 'rev-parse', 'HEAD')
    body += f'\nFetch https://github.com/example/project branch wt/fix and check out {head}.'
    return kp.portable_reason({'title': 'Fix parser', 'body': body, **extra}, {})


def test_clean_published_worktree_is_portable(published):
    assert reason('Continue in `~/code/_wt/fix`.') is None


@pytest.mark.parametrize('change', ['dirty', 'untracked', 'ahead', 'deleted', 'detached'])
def test_unreconstructible_worktree_stays_local(published, change):
    wt, origin = published
    if change in ('dirty', 'untracked'):
        (wt / 'local.txt').write_text('only here')
        if change == 'dirty':
            git(wt, 'add', 'local.txt')
    elif change == 'ahead':
        git(wt, 'commit', '--allow-empty', '-m', 'not pushed')
    elif change == 'deleted':
        git(origin, 'update-ref', '-d', 'refs/heads/wt/fix')
    else:
        git(wt, 'checkout', '--detach')
    assert reason('Continue in `~/code/_wt/fix`.') == 'home gateway marker'


@pytest.mark.parametrize('suffix', [' and ~/code/_wt/missing', ' and ~/code/_wt/fix-other', ' and kanban.db'])
def test_one_published_path_does_not_exempt_other_needs(published, suffix):
    assert reason('Continue in ~/code/_wt/fix' + suffix) == 'home gateway marker'


@pytest.mark.parametrize('suffix', ['.bak/x', '/../../secrets', '/.env', '/dist/local'])
def test_subpaths_and_dotted_siblings_stay_local(published, suffix):
    assert reason('Continue in ~/code/_wt/fix' + suffix) == 'home gateway marker'


def test_local_origin_is_not_portable(published):
    wt, origin = published
    git(wt, 'remote', 'set-url', 'origin', str(origin))
    assert reason('~/code/_wt/fix') == 'home gateway marker'


def test_rewritten_origin_stays_local(published):
    wt, origin = published
    git(wt, 'config', f'url.{origin}.insteadOf', 'https://github.com/example/project.git')
    assert reason('~/code/_wt/fix') == 'home gateway marker'


def test_missing_checkout_identity_stays_local(published):
    assert kp.portable_reason({'title': 'Fix parser', 'body': '~/code/_wt/fix'}, {}) == 'home gateway marker'


def test_other_marker_skips_git_probes(published, monkeypatch):
    from hermes_cli import kanban_published_worktree as pw
    monkeypatch.setattr(pw, '_published', lambda *a: pytest.fail('unnecessary probe'))
    assert reason('~/code/_wt/fix and kanban.db') == 'home gateway marker'


@pytest.mark.parametrize('change', ['non-wt', 'submodules', 'timeout', 'head-race', 'branch-race', 'dirty-race'])
def test_defensive_publication_fences(published, monkeypatch, change):
    wt, _ = published
    original = subprocess.check_output
    if change == 'non-wt':
        git(wt, 'branch', '-m', 'feature/fix')
    elif change == 'submodules':
        (wt / '.gitmodules').write_text('')
    else:
        def intercept(argv, **kw):
            result = original(argv, **kw)
            if 'ls-remote' in argv and '--exit-code' in argv:
                if change == 'timeout':
                    raise subprocess.TimeoutExpired(argv, 3)
                if change == 'head-race':
                    git(wt, 'commit', '--allow-empty', '-m', 'race')
                elif change == 'branch-race':
                    git(wt, 'branch', '-m', 'wt/other')
                elif change == 'dirty-race':
                    (wt / 'race').write_text('unpublished')
            return result
        monkeypatch.setattr(subprocess, 'check_output', intercept)
    assert reason('~/code/_wt/fix') == 'home gateway marker'


def test_request_scope_reuses_observation_and_resets(published, monkeypatch):
    from hermes_cli import kanban_published_worktree as pw
    calls = []
    original = pw._published

    def probe(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(pw, '_published', probe)

    @pw.publication_scope
    def nested():
        assert reason('~/code/_wt/fix') is None

    @pw.publication_scope
    def create():
        nested()
        nested()
        assert len(calls) == 1
        raise ValueError('test scope cleanup')

    with pytest.raises(ValueError, match='scope cleanup'):
        create()
    nested()
    assert len(calls) == 2
    assert pw._observation.get() is None


def test_scope_shares_deadline_across_different_texts(published, monkeypatch):
    from hermes_cli import kanban_published_worktree as pw
    deadlines = []
    monkeypatch.setattr(pw, '_published', lambda path, deadline, text: deadlines.append(deadline) or False)

    @pw.publication_scope
    def create():
        reason('~/code/_wt/fix')
        reason('another ~/code/_wt/fix')

    create()
    assert len(deadlines) == 2
    assert deadlines[0] == deadlines[1]


def test_expired_budget_fails_closed(published):
    from hermes_cli import kanban_published_worktree as pw
    assert not pw._published(published[0], 0, 'unused')


def test_structural_host_requirements_stay_local(published):
    assert reason('~/code/_wt/fix', workspace_kind='dir') == 'host workspace'
    assert reason('~/code/_wt/fix', requires=['host_repo']) == 'uncarried requirement'
