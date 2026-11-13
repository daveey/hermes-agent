"""A closed ``( … )`` group's output ends at the next operator (PR #2586 test gap).

``_LineWalk.operator`` resets ``self.closed`` along with ``self.closing``. Without that reset the
output of ``(true)`` outlived an operator whose segment was empty (``(true); …``), and the next
flush took ``pending = self.closed`` in place of the command's own output: the feed of
``cat r.sh | bash`` was lost and the guard failed open.
"""

from __future__ import annotations

import shlex

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])


@pytest.fixture
def files(tmp_path) -> dict[str, str]:
    sh = tmp_path / "r.sh"
    sh.write_text(f"#!/bin/sh\necho hi\n{LIFECYCLE}\n", encoding="utf-8")
    py = tmp_path / "r.py"
    py.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in {"sh": sh, "py": py}.items()}


@pytest.mark.parametrize(
    "template",
    [
        "(true); cat {sh} | bash",
        "(echo hi); cat {sh} | bash",
        "(true) && cat {sh} | bash",
        "(true) || cat {sh} | bash",
        "(true) & cat {sh} | bash",
        "(echo hi; true); cat {py} | python3",
        "( (true) ); cat {sh} | bash",
    ],
)
def test_closed_subshell_output_does_not_outlive_the_next_operator(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # The subshell's output went to its own `;`, not into the later pipe.
        "(cat {sh}); echo x | bash",
        "(cat {sh}) && echo x | bash",
    ],
)
def test_closed_subshell_output_does_not_feed_a_later_pipe(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)
