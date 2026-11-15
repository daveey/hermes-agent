"""Arithmetic text must not move a command frame's heredoc or case state."""

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_comment_words as helpers


@pytest.mark.parametrize("index", [
    "${b#]}1<<2", "$(echo ']')1<<2", "`echo ']'`1<<2", "$[1+2]<<2",
])
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_subscript_expansions_do_not_end_bracket_early(tmp_path, index, dangerous):
    helpers._tree(tmp_path)
    cmd = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    command = '(\necho "${x:-$(a[' + index + ']=3\n)}"; ' + cmd + '\n2]=3\n)'
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


@pytest.mark.parametrize("body", [
    "cat a[ <<E ]=x\ncase x in\nE\n",
    "echo $((\ncase x in\n))",
    "echo $[\ncase x in\n]",
    "((\ncase x in\n))",
    "a[\ncase x in\n]=3",
])
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_only_shell_words_move_case_and_heredoc_state(tmp_path, body, dangerous):
    helpers._tree(tmp_path)
    cmd = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    # Arithmetic errors terminate the substitution, not the enclosing shell.
    command = 'echo "${x:-$(' + body + '\n)}"; ' + cmd
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


@pytest.mark.parametrize("frame", [
    'echo "${x:-$(echo $(( $(true\n{cmd}\necho 0) + 1 )))}"',
    'echo "${x:-$(echo $[ $(true\n{cmd}\necho 0) + 1 ])}"',
    'echo "${x:-$(a[$(true\n{cmd}\necho 0)]=3)}"',
    'echo "${x:-$(a=( [$(true\n{cmd}\necho 0)]=3 ))}"',
    'echo "${x:-$(a[$[1+2]<<2]=3; cat <<E\ncase x in\nE\n)}"; {cmd}',
    'echo "${x:-$(a=( [1<<2]=3 ); case x in (case) true;; esac)}"; {cmd}',
])
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_arithmetic_keeps_nested_commands_and_later_shell_state(tmp_path, frame, dangerous):
    helpers._tree(tmp_path)
    cmd = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    command = frame.replace("{cmd}", cmd)
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


@pytest.mark.parametrize("assignment,tail", [
    ("a=( [1<<2]=3 )", "2]=3"),
    ("a+=( [1<<2]=3 )", "2]=3"),
    ("declare -A a=( [x<<2]=3 )", "2]=3"),
])
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_compound_assignment_shift_is_not_heredoc(tmp_path, assignment, tail, dangerous):
    helpers._tree(tmp_path)
    cmd = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    command = '(\necho "${x:-$(' + assignment + '\n)}"; ' + cmd + '\n' + tail + '\n)'
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason
