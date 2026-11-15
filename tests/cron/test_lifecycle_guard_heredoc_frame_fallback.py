"""Ambiguous heredocs must not extend a substitution past real shell commands."""

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_comment_words as helpers


ARITHMETIC = [
    ("echo $[1<<2]", "2]"),
    ("a[1<<2]=3", "2]=3"),
    ("a=( $[1<<2] )", "2]"),
    ("if((1<<2)); then :; fi", "2"),
    ("while((0<<2)); do :; done", "2"),
    ("!((1<<2))", "2"),
]
DELIMITERS = [
    ("$'E'", "E"),
    ('$"E"', "E"),
    ("$'E\\x41'", "EA"),
    ("`echo E`", "`echo E`"),
    ("'E\\'", "E\\"),
]
FRAMES = ['echo "${x:-$(BODY)}"', 'echo ${x:-"$(BODY)"}',
          'x="${x:-$(BODY)}"', 'echo "${x:-${y:-$(BODY)}}"']


@pytest.mark.parametrize("body,tail", ARITHMETIC)
@pytest.mark.parametrize("outer", [False, True], ids=["root", "subshell"])
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_arithmetic_shift_is_not_a_heredoc(tmp_path, body, tail, outer, dangerous):
    helpers._tree(tmp_path)
    cmd = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    command = 'echo "${x:-$(' + body + '\n)}"; ' + cmd + '\n' + tail
    if outer:
        command = '(\n' + command + '\n)'
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason


@pytest.mark.parametrize("delimiter,terminator", DELIMITERS)
@pytest.mark.parametrize("frame", FRAMES)
@pytest.mark.parametrize("dangerous", [True, False], ids=["RUN", "clean"])
def test_unrecognized_heredoc_does_not_open_phantom_case(
        tmp_path, delimiter, terminator, frame, dangerous):
    helpers._tree(tmp_path)
    body = 'cat <<' + delimiter + '\ncase x in\n' + terminator + '\n'
    cmd = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    command = frame.replace("BODY", body) + '; ' + cmd
    assert helpers._bash_runs(tmp_path, command) is dangerous
    refused, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert refused is dangerous, reason
