"""A Python-looking shebang is not proof of the executed interpreter.

Direct-wrapper compatibility remains open: these tests require conservative
shell scanning, not a path/name exemption. Only private fake Hermes runs.
"""
import ast
import random
import shlex
import shutil
import subprocess
import sys

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments.local import build_subprocess_env


@pytest.fixture
def shell_fixture(tmp_path):
    bash = shutil.which('bash')
    assert bash
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'bash').symlink_to(bash)
    (bin_dir / 'python3').symlink_to(bash)
    marker = tmp_path / 'marker'
    fake = bin_dir / 'hermes'
    fake.write_text('#!' + bash + '\nprintf "%s\\n" "$*" >> "$MARKER"\n', encoding='utf-8')
    fake.chmod(0o700)
    inner = tmp_path / 'inner.sh'
    inner.write_text('hermes gateway restart\n', encoding='utf-8')
    env = build_subprocess_env(
        base={}, inherit_profile_home=False, scrub_secrets=False,
        extra={'PATH': str(bin_dir), 'HOME': str(tmp_path),
               'HERMES_HOME': str(tmp_path / 'home'), 'TMPDIR': str(tmp_path),
               'LANG': 'C.UTF-8', 'MARKER': str(marker)},
    )
    return bash, bin_dir, marker, inner, env


def run_marker(command, tmp_path, fixture, expected):
    bash, _, marker, _, env = fixture
    marker.unlink(missing_ok=True)
    result = subprocess.run(
        [bash, '--noprofile', '--norc', '-c', command], cwd=tmp_path,
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=3,
        check=False,
    )
    # Bash can fail AFTER expansion; the marker, not exit status, is the oracle.
    assert marker.exists() is expected, (command, result.stderr)
    if expected:
        assert marker.read_text(encoding='utf-8') == 'gateway restart\n'
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('header_kind', ['absolute-symlink', 'env-path', 'env-split-path'])
def test_interpreter_shadow_requires_shell_scan(tmp_path, shell_fixture, header_kind):
    _, bin_dir, _, inner, _ = shell_fixture
    headers = {
        'absolute-symlink': '#!' + str(bin_dir / 'python3'),
        'env-path': '#!/usr/bin/env python3',
        'env-split-path': '#!/usr/bin/env -S python3 -u',
    }
    script = tmp_path / 'wrapper'
    text = headers[header_kind] + '\n"""`bash ' + str(inner) + '`"""\n'
    ast.parse(text)  # Even valid Python can execute substitutions under Bash.
    script.write_text(text, encoding='utf-8')
    script.chmod(0o700)
    direct = shlex.quote(str(script))
    # Pin the real Python executable; PATH's python3 is deliberately Bash.
    explicit = shlex.quote(sys.executable) + ' ' + direct
    run_marker(explicit, tmp_path, shell_fixture, False)
    assert scan_gateway_lifecycle(explicit) == (False, None)
    run_marker(direct, tmp_path, shell_fixture, True)
    assert scan_gateway_lifecycle(direct)[0] is True


@pytest.mark.parametrize('invocation', ['bash {p}', 'source {p}', '. {p}',
                                       'bash < {p}', 'cat {p} | bash', 'bash -l {p}'])
@pytest.mark.parametrize('direct_first', [False, True])
@pytest.mark.parametrize('remote', [False, True])
def test_forced_shell_visit_is_not_hidden(tmp_path, invocation, direct_first, remote):
    inner = tmp_path / 'inner.sh'
    inner.write_text('hermes gateway restart\n', encoding='utf-8')
    script = tmp_path / 'wrapper'
    text = '#!/usr/bin/env python3\n"""`bash ' + str(inner) + '`"""\n'
    if not remote:
        script.write_text(text, encoding='utf-8')
    p = shlex.quote(str(script))
    command = invocation.format(p=p)
    if direct_first:
        first = tmp_path / 'first.sh'
        first.write_text(p + '\n', encoding='utf-8')
        command = 'bash ' + shlex.quote(str(first)) + '; ' + command
    reader = lambda candidate: text if candidate == str(script) else None
    assert scan_gateway_lifecycle(command, read_remote_script=reader)[0] is True


@pytest.mark.parametrize('invocation', ['bash --rcfile {p} -i -c :',
                                       'bash --init-file {p} -i -c :', 'su -l nobody < {p}'])
def test_startup_and_nested_stdin_ignore_python_header(tmp_path, invocation):
    inner = tmp_path / 'inner.sh'
    inner.write_text('hermes gateway restart\n', encoding='utf-8')
    script = tmp_path / 'startup'
    script.write_text('#!/usr/bin/env python3\n"""`bash ' + str(inner) + '`"""\n',
                      encoding='utf-8')
    # Scan only: never launch an interactive shell, su, or a remote command.
    assert scan_gateway_lifecycle(invocation.format(p=shlex.quote(str(script))))[0]


@pytest.mark.platforms('linux')
def test_seeded_explicit_python_and_forced_shell_controls(tmp_path, shell_fixture):
    _, _, _, inner, _ = shell_fixture
    cases = []
    for index, header in enumerate(['', '#!/usr/bin/env python3',
                                    '#!/usr/bin/env -S python3 -u']):
        script = tmp_path / ('program-' + str(index))
        script.write_text(header + '\n"""`bash ' + str(inner) + '`"""\n', encoding='utf-8')
        p = shlex.quote(str(script))
        cases.append((shlex.quote(sys.executable) + ' -B ' + p, False))
        for command in [f'bash {p}', f'source {p}', f'bash < {p}']:
            cases.append((command, True))
    random.Random(2350).shuffle(cases)
    for command, expected in cases:
        run_marker(command, tmp_path, shell_fixture, expected)
        assert scan_gateway_lifecycle(command)[0] is expected, command
