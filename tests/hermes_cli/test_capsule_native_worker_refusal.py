"""Native, unmodified managed-worker entry refuses before hello or observation setup."""
import json
import os
import socket
import subprocess
import hashlib
import shutil

import pytest

from tests.hermes_cli import test_code_attestation_route as helpers

core = helpers.core
python = helpers.python
pytestmark = helpers.pytestmark


def test_actual_pre_v2_child_refuses_new_owner_argv(tmp_path, native_core, python):
    """Unmodified worker from b63da7870575d42b7bb851869218323dc513f48f.

    This is a frozen old entrypoint, not a new parser given an unknown v3 flag.
    The bootstrap/dependencies remain the committed native fixture environment.
    """
    from hermes_cli._launch_template import render
    old = tmp_path / 'old-core'
    shutil.copytree(native_core, old)
    source = (helpers.DATA / 'capsule_worker_pre_v2.py.txt').read_bytes()
    assert hashlib.sha256(source).hexdigest() == '03a8bbcba4e2605d9d8645582e723cc1305f8e2e35c401c5023dc6b4378fdabd'
    (old / 'agent' / 'managed_worker.py').write_bytes(source)
    helpers._git(old, 'add', '.')
    helpers._git(old, 'commit', '-qm', 'pre-v2-native-worker')
    home = tmp_path / 'home'
    home.mkdir()
    owner, child = socket.socketpair()
    try:
        result = subprocess.run([str(python), '-I', '-c', render(str(old), 'agent.managed_worker', 'main'),
            '--observation-fd', str(child.fileno()), '--capsule-contract-v2',
            helpers._git(old, 'rev-parse', 'HEAD').strip()], input=b'', capture_output=True,
            cwd=tmp_path, pass_fds=(child.fileno(),), timeout=30, check=False,
            env={'PATH': os.defpath, 'HOME': str(tmp_path), 'HERMES_HOME': str(home),
                 'TMPDIR': str(tmp_path), 'LANG': 'C.UTF-8'})
    finally:
        child.close()
        owner.close()
    assert result.returncode == 1
    assert b'invalid_managed_worker_argv' in result.stderr, result.stderr.decode(errors='replace')
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {'type': 'error', 'reason': 'managed_worker_failed'}]


@pytest.fixture(scope='module')
def native_core(core):
    (core / '.gitignore').write_text('__pycache__/\n*.pyc\n')
    helpers._git(core, 'add', '.')
    helpers._git(core, 'commit', '-qm', 'native-worker-fixture')
    return core


@pytest.mark.parametrize('case', ['wrong-sha', 'missing', 'unsupported', 'dirty-boot'])
def test_native_main_refuses_before_hello(tmp_path, native_core, python, case):
    from hermes_cli._launch_template import render
    sha = helpers._git(native_core, 'rev-parse', 'HEAD').strip()
    args = ['--observation-fd', '9', '--capsule-contract-v2', sha]
    if case == 'wrong-sha':
        args[-1] = '0' * 40
    elif case == 'missing':
        args = args[:2]
    elif case == 'unsupported':
        args[2] = '--capsule-contract-v3'
    source = native_core / 'capsule_dirty_fixture.py'
    if case == 'dirty-boot':
        source.write_text('VALUE = 1\n')
    home = tmp_path / 'profile'
    home.mkdir()
    try:
        result = subprocess.run([str(python), '-I', '-c',
            render(str(native_core), 'agent.managed_worker', 'main'), *args],
            input=b'', capture_output=True, cwd=tmp_path, timeout=30, check=False,
            env={'PATH': os.defpath, 'HOME': str(tmp_path), 'HERMES_HOME': str(home),
                 'TMPDIR': str(tmp_path), 'LANG': 'C.UTF-8'})
    finally:
        if source.exists():
            source.unlink()
    assert result.returncode == 1, result.stderr.decode(errors='replace')
    assert b'capsule_worker_code_unverified' in result.stderr
    # Production main returns its final error and exits without requesting a bootstrap,
    # adopting a session, importing an observer or constructing any model agent.
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert frames == [{'type': 'error', 'reason': 'managed_worker_failed'}]
    assert helpers._git(native_core, 'status', '--porcelain').strip() == ''


def test_owner_command_reaches_native_hello_with_verified_expectation(tmp_path, native_core, monkeypatch):
    from gateway import capsule_contract as contract
    monkeypatch.setattr(contract, 'CORE_ROOT', native_core)
    monkeypatch.setattr('gateway.session_kanban_capsule.check_capsule_transport', lambda policy: True)
    sha = helpers._git(native_core, 'rev-parse', 'HEAD').strip()
    home = tmp_path / 'profile'
    home.mkdir()
    owner, child = socket.socketpair()
    try:
        argv = contract.worker_command(object()) + ['--observation-fd', str(child.fileno()),
                                                    contract.WORKER_FLAG, sha]
        result = subprocess.run(argv, input=b'', capture_output=True, timeout=30, check=False,
            cwd=tmp_path, pass_fds=(child.fileno(),),
            env={'PATH': os.defpath, 'HOME': str(tmp_path), 'HERMES_HOME': str(home),
                 'TMPDIR': str(tmp_path), 'LANG': 'C.UTF-8'})
    finally:
        owner.close()
        child.close()
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 1  # no bootstrap supplied: intentionally no adopted session/model
    assert [frame['type'] for frame in frames] == ['hello', 'error'], result.stderr.decode(errors='replace')
    assert frames[0]['pid'] > 1 and frames[0]['birth']
    assert frames[1] == {'type': 'error', 'reason': 'managed_worker_failed'}
    assert b'capsule_worker_code_unverified' not in result.stderr

