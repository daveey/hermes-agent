"""A directly invoked Python-shebang file keeps its shell walk; only the computed-input
uncertainty check is withheld from its own text (so the verdict equals the pre-check one)."""
import shlex

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_shebang import is_python_shebang

UNCERTAIN = 'the shell input depends on unresolved command substitution output'
PYTHON_HEADERS = ['#!/usr/bin/env python3', '#!/usr/bin/python3', '#!/usr/bin/env -S python3 -u']
# The guard reads file text only. These lines stand for Python source (prose, backticks)
# that the shell walk reads as a shell consumer fed by substitution output.
DOC = 'import sys\nbash $(echo ./r.sh)\n'


def _script(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    path.chmod(0o700)
    return shlex.quote(str(path))


@pytest.mark.parametrize('header', PYTHON_HEADERS)
@pytest.mark.parametrize('wrapper', ['', 'command ', 'env ', 'timeout 3 ', 'nohup '])
def test_direct_python_shebang_documentation_is_not_newly_refused(tmp_path, header, wrapper):
    program = _script(tmp_path, 'tool', header + '\n' + DOC)
    assert scan_gateway_lifecycle(wrapper + program + " w -- 'x --dry-run'") == (False, None)


def test_compound_direct_invocations_stay_allowed(tmp_path):
    program = _script(tmp_path, 'tool', PYTHON_HEADERS[0] + '\n' + DOC)
    command = f"printf x | python3 -c 'import sys'; {program} a -- 'x'; {program} b -- 'x'"
    assert scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize('header', PYTHON_HEADERS)
def test_direct_python_shebang_is_never_promoted_past_the_shell_walk(tmp_path, header):
    # The interpreter word is not proof (PATH shadow): literal lifecycle text still refuses.
    program = _script(tmp_path, 'tool', header + '\nimport sys\nhermes gateway restart\n')
    assert scan_gateway_lifecycle(program) == (True, None)


@pytest.mark.parametrize('form', ['bash {p}', 'sh {p}', 'source {p}', '. {p}', 'bash < {p}',
                                  'cat {p} | bash', '{p}; bash {p}', 'bash {p}; {p}'])
def test_forced_shell_visits_of_the_same_file_keep_the_check(tmp_path, form):
    program = _script(tmp_path, 'tool', PYTHON_HEADERS[0] + '\n' + DOC)
    assert scan_gateway_lifecycle(form.format(p=program)) == (True, UNCERTAIN)


@pytest.mark.parametrize('header', ['#!/usr/bin/env bash', '#!/bin/sh', '', '#!/usr/bin/env python3 -c',
                                    '#!/usr/bin/env FOO=1 python3', '#!/usr/bin/env -S bash python3'])
def test_other_direct_files_keep_the_check(tmp_path, header):
    text = (header + '\n' if header else '') + 'bash $(echo ./r.sh)\n'
    program = _script(tmp_path, 'tool', text)
    assert not is_python_shebang(text)
    assert scan_gateway_lifecycle(program) == (True, UNCERTAIN)


def test_script_referenced_from_a_direct_python_file_is_not_covered(tmp_path):
    nested = _script(tmp_path, 'nested.sh', 'bash $(echo ./r.sh)\n')
    program = _script(tmp_path, 'tool', PYTHON_HEADERS[0] + f'\nimport sys\nbash {nested}\n')
    assert scan_gateway_lifecycle(program) == (True, UNCERTAIN)


def test_command_line_computed_input_next_to_a_direct_python_file_still_refuses(tmp_path):
    program = _script(tmp_path, 'tool', PYTHON_HEADERS[0] + '\n' + DOC)
    assert scan_gateway_lifecycle(f'{program}; bash $(echo ./r.sh)') == (True, UNCERTAIN)
    assert scan_gateway_lifecycle(f'bash $(echo {program})') == (True, UNCERTAIN)
