"""Computed executable words: real Bash RUN proofs, never real lifecycle calls."""
import os
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


RUN = [
    'x=$(cat command-name); ${x:-echo} gateway restart',
    'x=$(printf %s hermes); ${x:-echo} gateway restart',
    '$\\\n(echo hermes) gateway restart',
    ':() { printf hermes; }; $(:) gateway restart',
    '. ./colon.sh; $(:) gateway restart',
    '. ./dynamic.sh; ${x:-echo} gateway restart',
    'x=$(${x:-hermes} gateway restart)',
    'echo `${x:-hermes} gateway restart`',
    'echo "$(echo "$(${x:-bash} ./r.sh)")"',
    '((${x:-hermes} gateway restart); true)',
    'echo $(( ${x:-hermes} gateway restart); true)',
    '${x:-hermes gateway restart}',
    '${x:-hermes} gateway restart',
    '$(echo hermes) gateway restart',
    '${x:-bash ./r.sh}',
    '${x:-bash} ./r.sh',
    '`echo hermes` gateway restart',
    '$(printf %s hermes) gateway restart',
    'env ${x:-bash} ./r.sh',
    '"$(echo hermes)" gateway restart',
    '${x:-bash} < ./r.sh',
    'her$(echo mes) gateway restart',
    '$(echo her)$(echo mes) gateway restart',
    'x=hermes; ${x:-echo} gateway restart',
    '. ./values.sh; ${x:-echo} gateway restart',
    '$(cat command-name) gateway restart',
    'x=prefixhermes; ${x#prefix} gateway restart',
    'echo() { printf hermes; }; $(echo echo) gateway restart',
    '. ./producer.sh; $(echo echo) gateway restart',
]
CLEAN = [
    '$\\\n(echo echo) hi',
    "echo '$\\\n(echo hermes) gateway restart'",
    'echo hi # $\\\n(echo hi)',
    '$(:) echo hi',
    'x=$(cat command-name); echo ${x:-echo} gateway restart',
    'result=$(${x:-echo} hi)',
    'echo `${x:-echo} hi`',
    'echo "$(echo "$(${x:-bash} ./clean.sh)")"',
    '((${x:-echo} hi); true)',
    'echo $(( ${x:-echo} hi); true)',
    '${x:-echo} hi',
    '$(echo echo) hi',
    '$(printf %s echo) hi',
    '`echo echo` hi',
    '${x:-bash} ./clean.sh',
    '${x:-bash ./clean.sh}',
    "echo '${x:-hermes} gateway restart'",
    "echo '$(echo hermes) gateway restart'",
    'echo ${x:-hermes} gateway restart',
    'echo $(echo hermes) gateway restart',
    '$(true) echo hi',
    'echo "$(cat command-name)"',
    'x=$(( $(echo 1) + 1 )); echo "$x"',
    '(( $(echo 1) + 1 )); echo hi',
]


@pytest.mark.parametrize('command,run', [(s, True) for s in RUN] + [(s, False) for s in CLEAN])
def test_computed_executable_bash_differential(tmp_path, command, run):
    stub = tmp_path / 'hermes'
    stub.write_text('#!/bin/sh\nprintf RUN > "$MARKER"\n')
    stub.chmod(0o755)
    (tmp_path / 'r.sh').write_text('hermes gateway "restart"\n')
    (tmp_path / 'clean.sh').write_text('echo hi\n')
    (tmp_path / 'values.sh').write_text('x=hermes\n')
    (tmp_path / 'producer.sh').write_text('echo() { printf hermes; }\n')
    (tmp_path / 'command-name').write_text('hermes\n')
    (tmp_path / 'colon.sh').write_text(':() { printf hermes; }\n')
    (tmp_path / 'dynamic.sh').write_text('x=$(cat command-name)\n')
    marker = tmp_path / 'marker'
    env = {'PATH': str(tmp_path) + os.pathsep + '/usr/bin:/bin', 'MARKER': str(marker)}
    proof = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', command],
                           cwd=tmp_path, env=env, capture_output=True, timeout=10, check=False)
    assert proof.returncode == 0, proof.stderr
    assert marker.exists() == run
    if run:
        assert marker.read_text() == 'RUN'
    unsafe, _ = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe == run


@pytest.mark.parametrize('command', [
    '$(cat command-name) gateway restart', '${x#prefix} gateway restart',
    # The additive binding graph is not ordered shell state: a dynamic
    # self-assignment cannot prove its own default's set-value reading safe.
    'x=$(${x:-echo} hi)',
])
def test_unresolved_computed_executable_fails_closed(command, tmp_path):
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert refusal and 'computed' in refusal
