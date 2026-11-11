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


def test_structural_host_requirements_stay_local(published):
    assert reason('~/code/_wt/fix', workspace_kind='dir') == 'host workspace'
    assert reason('~/code/_wt/fix', requires=['host_repo']) == 'uncarried requirement'
