"""A computed program or directory prefix is scanned, not refused outright.

Refusing every ``$PY -c '...'`` and every ``. "$HERE/lib.sh"`` blocked the fleet's
own tools (``hermes-core-python``, ``scripts/run_tests.sh``, ``source ./activate``).
The literal part is still shell evidence: it is scanned, and anything the parent
could still change keeps the refusal.
"""
from __future__ import annotations

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_fd_redirections as fixtures

REASON = 'substitution output'

# Real bash runs the fake `hermes gateway restart`; the scan must refuse.
RUN = [
    "X=$(echo bash); $X -c './r.sh'",
    "X=$(echo bash); $X -c 'source ./r.sh'",
    "X=$(echo bash); $X -c 'hermes gateway restart'",
    "$(echo bash) -c 'hermes gateway restart'",
    "X=$(echo bash); nohup $X -c 'hermes gateway restart' >/dev/null 2>&1",
    'X=$(echo bash); S=./r.sh; $X -c "source $S"',
    "X=$(echo bash); export S=./r.sh; $X -c 'source $S'",
    "X=$(echo bash); $X -c 'bash' < ./r.sh",
    "X=$(echo bash); cat ./r.sh | $X -c 'bash'",
    "X=$(echo bash); exec 3< ./r.sh; $X -c 'bash <&3'",
    "X=$(echo bash); $X -c 'bash <&3' 3< ./r.sh",
    "X=$(echo bash); { $X -c 'bash'; } < ./r.sh",
    'X=$(echo bash); $X -c "$(cat ./r.sh)"',
    '. "$(pwd)/r.sh"',
    'H=$(pwd); . "$H/r.sh"',
    'source "$(echo .)/r.sh"',
    'bash "$(pwd)/r.sh"',
    'sh -e "$(pwd)/r.sh"',
    '. "$(pwd)/sub/../r.sh"',
    '. "$(pwd)/$(echo r.sh)"',
    '. "$(pwd)/sub/calls.sh"',
    '/bin/$(echo bash) < ./r.sh',
    'cat ./r.sh | /bin/$(echo bash)',
    '/bin/$(echo bash) ./r.sh',
]

# Real bash exits 0 without touching the marker; the scan must allow.
CLEAN = [
    "PY=$(command -v python3); $PY -c 'print(1)'",
    "PY=$(command -v python3); \"$PY\" -I -c 'import os\nprint(os.sep)' 2>&1 | tail -1",
    "PY=$(command -v python3) && timeout 20 $PY -c 'print(1)' > out.log 2>&1; echo rc=$?",
    'H=$(pwd); . "$H/lib.sh"',
    'H="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"; source "$H/sub/lib2.sh"',
    'bash "$(pwd)/lib.sh"',
    'account="/Users/$(id -un)"; echo "$account"',
    # An unrelated eval sink certifies every quote as a possible payload.
    'run() { eval "$1"; }; account="/Users/$(id -un)"; echo "$account"',
]


@pytest.fixture(autouse=True)
def _private_home_and_complete_walk(tmp_path, monkeypatch, caplog):
    """Resolve `~` and PATH inside the test tree, and reject a verdict that
    comes from the walker's failure fallback instead of from the scan."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', f"{tmp_path / 'bin'}:/usr/bin:/bin")
    yield
    assert 'walk failed' not in caplog.text


def _siblings(tmp_path):
    fixtures._tree(tmp_path)
    (tmp_path / 'r.sh').chmod(0o755)
    (tmp_path / 'lib.sh').write_text('FOO=1\n')
    home_bin = tmp_path / '.hermes' / 'bin'
    home_bin.mkdir(parents=True)
    (home_bin / 'hermes-core-wt').write_text('#!/bin/sh\necho "export CORE_WT=1"\n')
    (home_bin / 'hermes-core-wt').chmod(0o755)
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'lib2.sh').write_text('BAR=2\n')
    (tmp_path / 'sub' / 'calls.sh').write_text(f'bash {tmp_path}/r.sh\n')
    # Stand-ins for the fleet tool: the honest one prints an export line.
    for directory, body in (('bin', 'echo "export CORE_WT=1"'), ('evil', f'cat {tmp_path}/r.sh')):
        (tmp_path / directory).mkdir(exist_ok=True)
        tool = tmp_path / directory / 'hermes-core-wt'
        tool.write_text(f'#!/bin/sh\n{body}\n')
        tool.chmod(0o755)


@pytest.mark.parametrize('command', [
    'eval "$(hermes-core-wt env)"',
    'cd . && eval "$(hermes-core-wt env)" >/dev/null 2>&1; echo ok | tail -1',
    'export A="b c" D=e; eval "$(hermes-core-wt env)"; echo ok && echo ok',
])
def test_core_worktree_path_export_is_allowed(tmp_path, command):
    _siblings(tmp_path)
    assert not fixtures._bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    home = command.replace('hermes-core-wt env', '~/.hermes/bin/hermes-core-wt env')
    assert scan_gateway_lifecycle(home, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('command', [
    'hermes-core-wt() { cat ./r.sh; }; eval "$(hermes-core-wt env)"',
    'function hermes-core-wt { cat ./r.sh; }; eval "$(hermes-core-wt env)"',
    'PATH="$PWD/evil:$PATH"; eval "$(hermes-core-wt env)"',
    'export PATH="$PWD/evil:$PATH"; eval "$(hermes-core-wt env)"',
    'eval "$(hermes-core-wt env; cat ./r.sh)"',
    'eval "$(evil/hermes-core-wt env)"',
    'eval "$(./evil/hermes-core-wt env)"',
    'eval "$(cat ./r.sh)"',
])
def test_path_export_trust_does_not_cover_other_eval_input(tmp_path, command):
    _siblings(tmp_path)
    assert fixtures._bash_runs(tmp_path, command), 'bash must run the restart, or the row proves nothing'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('command', [
    'eval "$(hermes-core-wt env --json)"',
    'eval "$(hermes-core-wt env)" "$(cat ./r.sh)"',
    'eval "$(hermes-core-wt env)" > /dev/null "$(cat ./r.sh)"',
    'eval $(hermes-core-wt env)',
    'eval "$(hermes-core-wt  env)"',
    'HOME=/tmp/x; eval "$(~/.hermes/bin/hermes-core-wt env)"',
    'eval "$(/tmp/.hermes/bin/hermes-core-wt env)"',
    'noteval "$(hermes-core-wt env)"; bash -c "$(hermes-core-wt env)"',
])
def test_path_export_trust_is_exact(tmp_path, command):
    _siblings(tmp_path)
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and REASON in reason, (unsafe, reason)


@pytest.mark.parametrize('command', RUN)
def test_real_shell_runs_and_scan_refuses(tmp_path, command):
    _siblings(tmp_path)
    assert fixtures._bash_runs(tmp_path, command), 'bash must run the restart, or the row proves nothing'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('command', CLEAN)
def test_clean_computed_program_and_sibling_are_allowed(tmp_path, command):
    _siblings(tmp_path)
    marker = tmp_path / 'marker'
    result = subprocess.run(
        ['/bin/bash'], input=command + '\n', text=True, encoding='utf-8',
        capture_output=True, timeout=20, check=False, cwd=tmp_path,
        env={'PATH': f"{tmp_path / 'bin'}:/usr/bin:/bin", 'MARKER': str(marker),
             'HOME': str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('command', [
    '. "$(pwd)/missing.sh"',
    '. "$(pwd)/sub/../lib.sh"',
    'bash "$(pwd)/missing.sh"',
    '. "$(pwd)/"',
    '. "$(pwd)/$X"',
    '. "$(pwd)/*.sh"',
    'bash $(echo -c) "$(pwd)/lib.sh"',
    "X=$(echo bash); $X $(echo -c) 'true' < ./lib.sh",
    "X=$(echo bash); $X -s < ./lib.sh",
])
def test_unresolved_sibling_or_option_stays_uncertain(tmp_path, command):
    _siblings(tmp_path)
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason and REASON in reason


def test_known_limit_a_directory_prefix_is_not_followed(tmp_path):
    """Only the walk's own directory is searched for the literal tail. A prefix
    that evaluates elsewhere is not followed, as before this check existed."""
    _siblings(tmp_path)
    assert scan_gateway_lifecycle('. "$(echo /nonexistent)/lib.sh"', cwd=str(tmp_path)) == (False, None)
