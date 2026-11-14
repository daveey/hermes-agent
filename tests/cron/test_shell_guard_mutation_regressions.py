"""Behavioral regressions for expanding heredocs and comment tokenization."""

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


@pytest.mark.parametrize("body", ["$(printf data)", "`printf data`"])
def test_expanding_heredoc_substitution_fails_closed(body):
    # An unquoted delimiter expands the body even for an inert consumer. Do not
    # skip its substitutions as literal data when deciding whether output is code.
    command = f"cat <<EOF\n{body}\nEOF\n"
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)


@pytest.mark.parametrize("comment", ["", " # it's only a search"])
def test_comment_apostrophe_does_not_turn_grep_data_into_lifecycle(comment, tmp_path):
    # shlex must not parse quotes inside comments: its failure fallback otherwise
    # mistakes the quoted grep pattern for an executed gateway restart.
    command = "grep 'hermes gateway restart' /dev/null" + comment
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_comment_after_real_lifecycle_command_does_not_hide_it(tmp_path):
    unsafe, _ = scan_gateway_lifecycle(
        "hermes gateway restart # it's a real command", cwd=str(tmp_path),
    )
    assert unsafe
