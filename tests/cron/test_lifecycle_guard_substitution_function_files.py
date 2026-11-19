"""Function-defined literal-file descriptor effects must reach computed commands.

`f() { exec 0<./r.sh; }; f; $(printf bash)` runs bash on r.sh in real Bash; the
function effect saved only command-output bindings, dropping the file input.
Fake hermes only (helpers._tree installs a marker-writing stub).
"""
import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_substitution_output import computed_shell_input
from tests.cron import test_lifecycle_guard_fd_redirections as helpers

RUNS = [
    'f() { exec 0<./r.sh; }; f; $(printf bash)',
    'function f { exec 0<./r.sh; }; f; $(printf bash)',
    'f() { exec 0<./r.sh; }; f; `printf bash`',
    'f() { exec 0<./r.sh; }; g() { f; }; g; $(printf bash)',
    'f() { exec 0<./r.sh; }; g() { f; }; f; g; $(printf bash)',
    'f() { exec 0<./clean.py; }; g() { exec 0<./r.sh; }; f; g; $(printf bash)',
]
CLEAN = [
    'f() { exec 0<./r.sh; }; f; exec 0<&-; $(printf bash)',
]


@pytest.mark.parametrize('command', RUNS)
def test_function_file_binding_reaches_computed_command(tmp_path, command):
    helpers._tree(tmp_path)
    assert helpers._bash_runs(tmp_path, command)
    assert computed_shell_input(command, frozenset())[0]
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('command', CLEAN)
def test_function_file_binding_not_applied_when_closed(tmp_path, command):
    helpers._tree(tmp_path)
    assert not helpers._bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
