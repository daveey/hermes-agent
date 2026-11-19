"""Direct Python-shebang files keep the shell walk minus the computed-input check.

All operational payloads are scanned as text only, never dispatched.
"""
import shlex

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


DOC = '''"""The caller's documentation.
This is NOT "unreachable"; this is the route that works with the caller's
credentials.
Targets come from `helper`; `$VAR`, quotes and heredocs travel as written.
The caller's arguments remain data.
"""
print(1)
note = 'bash -c "`helper`"'
'''
LIFECYCLE = ' '.join(['hermes', 'gateway', 'restart'])


@pytest.mark.parametrize('shebang', ['#!/usr/bin/python3', '#!/usr/bin/env python3',
                                     '#!/usr/bin/env -S python3 -u'])
@pytest.mark.parametrize('prefix', ['', 'env ', 'timeout 10 ', 'command ', 'nohup '])
def test_direct_python_docstring_is_not_shell(tmp_path, shebang, prefix):
    path = tmp_path / 'wrapper'
    path.write_text(shebang + '\n' + DOC)
    assert scan_gateway_lifecycle(prefix + shlex.quote(str(path))) == (False, None)


@pytest.mark.parametrize('invocation', ['bash {p}', 'source {p}', '. {p}',
                                       'bash < {p}', 'cat {p} | bash', 'bash -l {p}'])
@pytest.mark.parametrize('direct_first', [False, True])
@pytest.mark.parametrize('remote', [False, True])
def test_shebang_does_not_hide_forced_shell_visit(tmp_path, invocation, direct_first, remote):
    # Python sees a string. Bash expands its backticks; the guard must walk inner.sh.
    inner = tmp_path / 'inner.sh'
    inner.write_text(LIFECYCLE + '\n')
    path = tmp_path / 'wrapper'
    text = '#!/usr/bin/env python3\n"""`bash ' + str(inner) + '`"""\n'
    if not remote:
        path.write_text(text)
    quoted = shlex.quote(str(path))
    command = invocation.format(p=quoted)
    if direct_first:
        # Force the direct visit before the outer command's forced-shell candidate walk.
        first = tmp_path / 'first.sh'
        first.write_text(quoted + '\n')
        command = 'bash ' + shlex.quote(str(first)) + '; ' + command
    reader = lambda candidate: text if candidate == str(path) else None
    assert scan_gateway_lifecycle(command, read_remote_script=reader)[0] is True


@pytest.mark.parametrize('remote', [False, True])
def test_direct_python_preserves_lifecycle_scan(tmp_path, remote):
    path = tmp_path / 'wrapper'
    text = '#!/usr/bin/env python3\nimport os\nos.system(' + repr(LIFECYCLE) + ')\n'
    if not remote:
        path.write_text(text)
    reader = lambda candidate: text if candidate == str(path) else None
    assert scan_gateway_lifecycle(str(path), read_remote_script=reader)[0] is True


@pytest.mark.parametrize('shebang', ['', '#!' + '/bin/bash', '#!/usr/bin/env unknown',
                                     '#!/usr/bin/env -S python3 -c pass',
                                     ' #!/usr/bin/python3'])
def test_unproven_shebang_keeps_shell_walk(tmp_path, shebang):
    path = tmp_path / 'wrapper'
    path.write_text(shebang + '\n' + DOC)
    assert scan_gateway_lifecycle(str(path))[0] is True


def test_remote_direct_wrapper_and_forbidden_argument(tmp_path):
    path = tmp_path / 'remote-wrapper'
    reader = lambda candidate: '#!/usr/bin/env python3\n' + DOC if candidate == str(path) else None
    safe = str(path) + " worker -- 'hermes kanban dispatch --dry-run --json'"
    assert scan_gateway_lifecycle(safe, read_remote_script=reader) == (False, None)
    assert scan_gateway_lifecycle(str(path) + ' worker -- ' + shlex.quote(LIFECYCLE),
                                  read_remote_script=reader)[0] is True


@pytest.mark.parametrize('line', [
    '#!/usr/bin/python3.12', '#!/usr/local/bin/python', '#!/usr/bin/pypy3',
    '#!/usr/bin/python3 -u', '#!/usr/bin/env python3',
    '#!/usr/bin/env -S python3 -u -B -E -I -s -S',
])
def test_python_shebang_allowlist(line):
    from cron.lifecycle_guard_shebang import is_python_shebang

    assert is_python_shebang(line + '\n' + DOC)


@pytest.mark.parametrize('line', [
    '', '#!', '#!   ', '#!/bin/sh', ' #!/usr/bin/python3',
    '#!/usr/bin/env', '#!/usr/bin/env -S', '#!/usr/bin/env -i python3',
    '#!/usr/bin/env python3 -u', '#!/usr/bin/env -S python3 -m os',
    '#!/usr/bin/env -S python3 -c pass', '#!/usr/bin/env -S python3 script.py',
    '#!/usr/bin/env -S python3 --unknown', '#!/usr/bin/env FOO=1 python3',
    '#!/usr/bin/env -S "python3"', '#!/usr/bin/env -S $PYTHON',
    '#!/usr/bin/python3-not', '#!/usr/bin/python3.exe',
])
def test_unknown_shebang_is_not_certified(line):
    from cron.lifecycle_guard_shebang import is_python_shebang

    assert not is_python_shebang(line + '\n' + DOC)


def test_direct_python_retains_data_sink_masking(tmp_path):
    path = tmp_path / 'looks-like-shell.sh'
    path.write_text('#!/usr/bin/env python3\n' +
                    f'"""Usage notes:\n    grep -c \'{LIFECYCLE}\' gateway.log\n"""\n')
    assert scan_gateway_lifecycle(str(path)) == (False, None)


def test_direct_unknown_contents_still_fail_closed(monkeypatch, tmp_path):
    from cron import lifecycle_guard

    monkeypatch.setattr(lifecycle_guard, '_MAX_REFERENCED_SCRIPT_BYTES', 100)
    path = tmp_path / 'big-wrapper'
    path.write_text('#!/usr/bin/env python3\n' + '# data\n' * 100)
    unsafe, refusal = scan_gateway_lifecycle(str(path))
    assert unsafe and refusal and str(path) in refusal
