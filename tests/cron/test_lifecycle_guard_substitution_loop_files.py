"""File-fed computed commands need descriptor facts from earlier iterations."""
import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_substitution_output import computed_shell_input
from tests.cron import test_lifecycle_guard_fd_redirections as helpers


@pytest.mark.parametrize('loop', ['for i in 1 2', 'i=0; while ((i++ < 2))'])
def test_file_binding_reaches_computed_command_on_backedge(tmp_path, loop):
    helpers._tree(tmp_path)
    command = f'{loop}; do $(printf bash); exec 0<./r.sh; done'
    assert helpers._bash_runs(tmp_path, command)
    assert computed_shell_input(command, frozenset())[0]
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('consumer', ['python3', 'cat'])
def test_loop_file_binding_does_not_taint_known_data_consumers(tmp_path, consumer):
    helpers._tree(tmp_path)
    (tmp_path / 'data').write_text('pass\n', encoding='utf-8')
    command = f'for i in 1 2; do {consumer}; exec 0<./data; echo $(true); done'
    assert not helpers._bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_loop_final_close_removes_file_input(tmp_path):
    helpers._tree(tmp_path)
    command = 'for i in 1 2; do exec 0<&-; $(printf bash); exec 0<./r.sh; done'
    assert not helpers._bash_runs(tmp_path, command)
    assert computed_shell_input(command, frozenset())[0] is False
