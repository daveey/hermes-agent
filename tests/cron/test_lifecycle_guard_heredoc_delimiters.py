"""Heredoc delimiters spelled with expansions or ``$'…'``/``$"…"`` quoting (PR #2942 review).

Bash reads a delimiter word through its expansions without performing them: ``cat <<$(echo E)``
ends at a line reading ``$(echo E)``, while ``<<$'\\x45'`` ends at ``E``. The opener parser
used to stop such a word at its first ``(`` or space (or keep the ``$`` of ``$'E'``), so the
body had no terminator; its lines were walked as commands of the script around it and a ``}``
there closed the outer group early, hiding ``cat f.py`` from the ``} | python3`` it feeds.
Each shape is checked against real bash too, with a harmless marker in place of the program.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import _parse_heredoc_operator

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])

# (delimiter word as written, the body line bash ends the heredoc at)
DELIMITERS = [
    ("$(echo E)", "$(echo E)"),
    ("`echo E`", "`echo E`"),
    ("$((1+2))", "$((1+2))"),
    ("${x:-a b}", "${x:-a b}"),
    ("$(echo ')')", "$(echo ')')"),
    ("a$(echo E)b", "a$(echo E)b"),
    ("$'E'", "E"),
    ("$'\\x45'", "E"),
    ('$"E"', "E"),
]


def _group(delimiter: str, terminator: str, inner: str, sink: str) -> str:
    return f"{{\ncat <<{delimiter}\n}}\n{terminator}\n{inner}\n}} | {sink}"


@pytest.mark.parametrize(("delimiter", "terminator"), DELIMITERS)
def test_group_feed_after_expansion_delimiter_is_scanned(tmp_path, delimiter, terminator):
    script = tmp_path / "f.py"
    script.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    command = _group(delimiter, terminator, f"cat {shlex.quote(str(script))}", "python3")
    assert scan_gateway_lifecycle(command) == (True, None)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("delimiter", "terminator"), DELIMITERS)
def test_bash_ends_the_body_where_the_parser_does(delimiter, terminator):
    """Bash itself ends the body at ``terminator``: the group's later command reaches the pipe."""
    command = _group(delimiter, terminator, "echo MARKER_FED", "cat")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                            timeout=20, check=False)
    assert "MARKER_FED" in result.stdout.splitlines(), result.stderr
    parsed = _parse_heredoc_operator(f"<<{delimiter}", 0)
    assert parsed is not None and parsed[1] == terminator


@pytest.mark.parametrize(
    ("opener", "delimiter", "quoted"),
    [
        ("<<$(echo E)", "$(echo E)", False),
        ("<<-$(echo E)", "$(echo E)", False),
        ("<<`echo E`", "`echo E`", False),
        ("<<$(echo \"(\" ')')", "$(echo \"(\" ')')", False),
        ("<<${x:-a b}", "${x:-a b}", False),
        ("<<$'a\\'b'", "a'b", True),
        ("<<$'\\t'", "\t", True),
        ('<<$"E"', "E", True),
        ("<<E$(x)'q'", "E$(x)q", True),
    ],
)
def test_opener_takes_expansion_parts_whole(opener, delimiter, quoted):
    parsed = _parse_heredoc_operator(opener, 0)
    assert parsed is not None
    end, word, _strip_tabs, is_quoted = parsed
    assert (end, word, is_quoted) == (len(opener), delimiter, quoted)


@pytest.mark.parametrize("opener", ["<<$(echo E", "<<`echo E", "<<${x", "<<$'E", "<<$(echo\nE)"])
def test_opener_with_unclosed_expansion_stays_unparsed(opener):
    assert _parse_heredoc_operator(opener, 0) is None
