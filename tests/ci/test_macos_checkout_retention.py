"""Exercise the actual pre-checkout program with disposable CI paths."""
import ast
from pathlib import Path
import shutil
import subprocess

import pytest
import hermes_yaml as yaml

ROOT = Path(__file__).resolve().parents[2]


def steps():
    workflow = yaml.safe_load((ROOT / '.github/workflows/hermes-state-macos.yml').read_text())
    return workflow['jobs']['hermes-state-macos']['steps']


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    first = steps()[0]
    assert first['name'] == 'Recreate dedicated CI checkout'
    assert first['working-directory'] == '${{ runner.temp }}'
    tree = ast.parse(first['run'])
    assert isinstance(tree.body[-1], ast.Expr)
    assert ast.unparse(tree.body[-1]) == 'prepare()'
    # Load definitions, then replace only the hard-coded runner roots with a temp tree.
    tree.body.pop()
    namespace = {}
    exec(compile(tree, '<workflow-cleanup>', 'exec'), namespace)
    root = tmp_path / 'ci-runner-hermes-state'
    namespace['ROOTS'] = (root,)
    work = root / '_work'
    current = work / 'daveey-hermes/daveey-hermes'
    old = work / 'hermes'
    for path in (current, old, work / '_temp', work / '_actions', work / '_tool'):
        path.mkdir(parents=True)
        (path / 'sentinel').write_text('keep until boundary')
    for key, value in {
        'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'macOS',
        'GITHUB_REPOSITORY': 'softmax-universe/daveey-hermes',
        'GITHUB_WORKSPACE': str(current), 'RUNNER_TEMP': str(work / '_temp'),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(work / '_temp')
    calls = []

    def unused(argv, **kwargs):
        assert argv[:3] == ['/usr/sbin/lsof', '-nP', '+D']
        assert kwargs['timeout'] == 120
        calls.append(Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 1, '', '')

    monkeypatch.setattr(subprocess, 'run', unused)
    return namespace['prepare'], current, old, calls


@pytest.mark.parametrize('previous_result', ['success', 'failure', 'cancelled'])
def test_next_start_recreates_checkout_and_removes_old_namespace(boundary, previous_result):
    prepare, current, old, calls = boundary
    for path in (current / '.git/objects/pack', old / 'hermes/.git/objects/pack'):
        path.mkdir(parents=True)
        (path / f'{previous_result}.pack').write_bytes(b'old pack')
    prepare()
    assert list(current.iterdir()) == []
    assert not old.exists()
    assert calls == [current, old]
    work = old.parent
    for name in ('_temp', '_actions', '_tool'):
        assert (work / name / 'sentinel').exists()
    prepare()  # No obsolete namespace is also valid.
    assert list(current.iterdir()) == []


@pytest.mark.parametrize('target_index', [0, 1])
@pytest.mark.parametrize('result', [(0, 'open file', ''), (1, '', 'warning'),
                                   (2, '', ''), (1, 'unexpected', '')])
def test_busy_or_uncertain_probe_preserves_both_targets(boundary, monkeypatch, target_index, result):
    prepare, current, old, _ = boundary

    def probe(argv, **kwargs):
        code, out, err = result if argv[-1] == str((current, old)[target_index]) else (1, '', '')
        return subprocess.CompletedProcess(argv, code, out, err)

    monkeypatch.setattr(subprocess, 'run', probe)
    with pytest.raises(RuntimeError, match='busy or lsof uncertain'):
        prepare()
    assert (current / 'sentinel').exists()
    assert (old / 'sentinel').exists()


@pytest.mark.parametrize('error', [FileNotFoundError('lsof'), subprocess.TimeoutExpired('lsof', 120)])
def test_probe_failures_preserve_checkout(boundary, monkeypatch, error):
    prepare, current, old, _ = boundary

    def probe(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, 'run', probe)
    with pytest.raises(type(error)):
        prepare()
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


@pytest.mark.parametrize('key', ['GITHUB_ACTIONS', 'RUNNER_OS', 'GITHUB_REPOSITORY',
                                  'GITHUB_WORKSPACE', 'RUNNER_TEMP'])
def test_explicit_ci_allowlist(boundary, monkeypatch, key):
    prepare, current, old, calls = boundary
    monkeypatch.setenv(key, '/unrelated/user/path')
    with pytest.raises(RuntimeError, match='outside dedicated CI'):
        prepare()
    assert not calls
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


def test_real_roots_are_the_mini_agent_and_room_mini_daemons():
    tree = ast.parse(steps()[0]['run'])
    tree.body.pop()
    namespace = {}
    exec(compile(tree, '<workflow-cleanup>', 'exec'), namespace)
    assert namespace['ROOTS'] == (Path('/Users/daveey/ci-runner-hermes-state'),
                                  Path('/Users/bbci/actions-runner-hermes'))


def test_runner_from_a_later_listed_root_cleans_its_own_checkout(boundary, tmp_path):
    # A room mini's runner root is listed after the mini's: selection follows
    # RUNNER_TEMP, not list order.
    prepare, current, old, calls = boundary
    prepare.__globals__['ROOTS'] = (tmp_path / 'other-runner', *prepare.__globals__['ROOTS'])
    prepare()
    assert list(current.iterdir()) == []
    assert not old.exists()
    assert calls == [current, old]


def test_runner_from_an_unlisted_root_is_refused(boundary, tmp_path):
    prepare, current, old, calls = boundary
    prepare.__globals__['ROOTS'] = (tmp_path / 'other-runner',)
    with pytest.raises(RuntimeError, match='outside dedicated CI'):
        prepare()
    assert not calls
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


@pytest.mark.parametrize('ancestor', [False, True])
def test_symlink_target_or_ancestor_refused(boundary, tmp_path, ancestor):
    prepare, current, old, _ = boundary
    alias = current.parent if ancestor else old
    moved = tmp_path / 'preserved'
    alias.rename(moved)
    alias.symlink_to(moved, target_is_directory=True)
    with pytest.raises(RuntimeError, match='symlink'):
        prepare()
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


def test_wrong_cwd_and_unsafe_rmtree_refused(boundary, monkeypatch):
    prepare, current, old, _ = boundary
    monkeypatch.setattr(shutil.rmtree, 'avoids_symlink_attacks', False)
    with pytest.raises(RuntimeError, match='fd-safe'):
        prepare()
    monkeypatch.setattr(shutil.rmtree, 'avoids_symlink_attacks', True)
    monkeypatch.chdir(current)
    with pytest.raises(RuntimeError, match='runner temp cwd'):
        prepare()
    assert (old / 'sentinel').exists()


def test_non_directory_refused_before_deletion(boundary):
    prepare, current, old, _ = boundary
    shutil.rmtree(old)
    old.write_text('not a directory')
    with pytest.raises(RuntimeError, match='non-directory'):
        prepare()
    assert (current / 'sentinel').exists()


def test_deletion_failure_aborts(boundary, monkeypatch):
    prepare, current, old, _ = boundary

    def refuse(path):
        raise PermissionError(str(path))

    monkeypatch.setattr(shutil, 'rmtree', refuse)
    refuse.avoids_symlink_attacks = True
    with pytest.raises(PermissionError):
        prepare()
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


@pytest.mark.parametrize('target_index', [0, 1])
def test_root_mount_refused_before_either_target_deleted(boundary, monkeypatch, target_index):
    prepare, current, old, _ = boundary
    mount = (current, old)[target_index]
    monkeypatch.setattr(Path, 'is_mount', lambda self: self == mount)
    with pytest.raises(RuntimeError, match='root mount'):
        prepare()
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


def test_nested_mount_refused(boundary, monkeypatch):
    prepare, current, old, _ = boundary
    mount = old / 'mounted'
    mount.mkdir()
    monkeypatch.setattr(Path, 'is_mount', lambda self: self == mount)
    with pytest.raises(RuntimeError, match='nested mount'):
        prepare()
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


def test_foreign_device_refused(boundary, monkeypatch):
    import os

    prepare, current, old, _ = boundary
    real_stat = Path.stat

    def stat(path, **kwargs):
        result = real_stat(path, **kwargs)
        if path == old:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(RuntimeError, match='foreign device'):
        prepare()
    assert (current / 'sentinel').exists() and (old / 'sentinel').exists()


def test_inner_symlink_does_not_delete_external_tree(boundary, tmp_path):
    prepare, _current, old, _ = boundary
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'valuable').write_text('preserve')
    (old / 'alias').symlink_to(outside, target_is_directory=True)
    prepare()
    assert (outside / 'valuable').read_text() == 'preserve'


def test_boundary_and_measurement_wiring():
    workflow_steps = steps()
    assert 'actions/checkout@' in workflow_steps[1]['uses']
    assert workflow_steps[1]['with']['fetch-depth'] == 1
    assert workflow_steps[0]['shell'] == '/usr/bin/python3 {0}'
    measure = workflow_steps[-1]
    assert measure['if'] == 'always()'
    assert '2097152' in measure['run']
    assert 'du -sk "$GITHUB_WORKSPACE"' in measure['run']
