"""Real-Bash boundary probes for expansion normalization and delimiter matching."""
from __future__ import annotations

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_substitutions import SUBSTITUTION, separate_substitutions
from tests.cron.test_lifecycle_guard_fd_redirections import _bash_runs, _tree


@pytest.mark.parametrize('prefix', ["echo 'a\\' $'b\\'c'", "echo $'b\\'c' 'a\\'"])
def test_ansi_normalization_keeps_original_line_evidence(tmp_path, prefix):
    _tree(tmp_path)
    command = prefix + '\nbash -<./r.sh'
    assert _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]
    clean = command.replace('bash -<./r.sh', 'python3 -<./clean.py')
    assert not _bash_runs(tmp_path, clean)
    assert scan_gateway_lifecycle(clean, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('pattern', [r'[\)]', r'[\(]', r'[]\)]', r'[\(\)x]', r'[!\(\)]'])
def test_bracket_pattern_parentheses_do_not_end_substitution(tmp_path, pattern):
    _tree(tmp_path)
    # The pattern need not match: the command after the case must still execute.
    body = f'case x in ({pattern}) true;; esac; bash ./r.sh'
    command = f'echo "$({body})"; echo after'
    assert _bash_runs(tmp_path, command)
    assert separate_substitutions(command) == (f'echo "{SUBSTITUTION}"; echo after', [body])
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('payload', [')', '(', '`', '"', "'", '$(not-a-command)', 'case x in x)'])
@pytest.mark.parametrize('redirect', ["<<'EOF'", '<<\\EOF', "<<-'EOF'"])
def test_heredoc_data_does_not_change_substitution_boundary(tmp_path, payload, redirect):
    _tree(tmp_path)
    body = f'cat {redirect}\n{payload}\nEOF\nbash ./r.sh'
    command = f'echo "$({body})"; echo after'
    assert _bash_runs(tmp_path, command)
    assert separate_substitutions(command) == (f'echo "{SUBSTITUTION}"; echo after', [body])
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]


@pytest.mark.parametrize('command', [
    'bash $(true) "./r.sh"',
    'bash $(true) "./r.sh" $(true)',
    'bash -s $(true) "present" < ./r.sh',
    '$(true) "bash" $(true) "./r.sh"',
    'bash $(true) "" -s < ./r.sh',
])
def test_empty_expansions_mixed_with_present_quoted_arguments(tmp_path, command):
    _tree(tmp_path)
    # A quoted empty argument before -s is a filename, not an absent word.
    runs = _bash_runs(tmp_path, command)
    if '"" -s' in command:
        assert not runs
        assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
        return
    assert runs
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0]
