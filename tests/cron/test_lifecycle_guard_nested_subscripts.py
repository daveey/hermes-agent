"""Nested arithmetic brackets must not turn shifts into here-documents."""

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron import test_lifecycle_guard_comment_words as h


@pytest.mark.parametrize("body,tail", [
    ("a[b[0]<<2]=3", "2]=3"),
    ("echo $[a[0]<<2]", "2]"),
    ("a[b[c[0]]<<2]=3", "2]=3"),
    ("echo $[a[b[0]]<<2]", "2]"),
])
@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("dangerous", [True, False])
def test_nested_subscripts_do_not_hide_following_command(tmp_path, body, tail, outer, dangerous):
    h._tree(tmp_path)
    following = "bash ./r.sh" if dangerous else "python3 ./clean.py"
    command = 'echo "${x:-$(' + body + '\n)}"; ' + following + '\n' + tail
    if outer:
        command = '(\n' + command + '\n)'
    assert h._bash_runs(tmp_path, command) is dangerous
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is dangerous, repr(command)
