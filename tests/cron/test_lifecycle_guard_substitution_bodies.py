"""Command-substitution bodies are walked for the scripts they run (card t_01a1d88a97332830).

Found in the review of PR #2925: bash runs ``./r.sh`` in each RUN row, but the guard never read a
backtick body for referenced scripts, and a double-quoted ``$(…)`` (bare, in a ``${x:-…}``
default or quoted inside one) was one shlex word, so its ``bash ./r.sh`` command (after a ``;`` or
a newline) was never a segment. Each row runs under real bash with a fake ``hermes`` that writes a
marker; each clean twin runs ``python3 ./clean.py`` in the same position and stays allowed.
"""

from __future__ import annotations

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_comment_words import _bash_runs, _tree

RUN = [
    "echo `bash ./r.sh`",
    'echo "`bash ./r.sh`"',
    "echo `true; bash ./r.sh`",
    "echo ${x:-`bash ./r.sh`}",
    "echo `true\nbash ./r.sh`",
    'echo "$(true; bash ./r.sh)"',
    'echo "$(true\nbash ./r.sh)"',
    'echo "${x:-$(true\nbash ./r.sh)}"',
    'echo ${x:-"$(true\nbash ./r.sh)"}',
]
ALLOWED = [row.replace("bash ./r.sh", "python3 ./clean.py") for row in RUN]


@pytest.mark.parametrize("command", RUN)
def test_script_run_inside_a_command_substitution_is_refused(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command), "bash must run the restart, or the row proves nothing"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


@pytest.mark.parametrize("command", ALLOWED)
def test_clean_script_inside_a_command_substitution_stays_allowed(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
