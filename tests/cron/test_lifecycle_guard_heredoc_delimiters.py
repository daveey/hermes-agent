"""Heredoc delimiters spelled with expansions or ``$'…'``/``$"…"`` quoting (PR #2942 review).

Bash reads most of a delimiter word through its expansions without performing them: backticks,
``$((…))`` and ``${…}`` end the body at a line spelled exactly like them, and ``<<$'\\x45'`` at
``E``. A ``$(…)`` is different: bash ends the body at its own reprint of the parsed command
(``<<$(echo  E|cat)`` at ``$(echo E | cat)``), even inside double quotes. The opener parser
used to stop such a word at its first ``(`` or space, so the body had no terminator; its lines
were walked as commands of the script around it, and a ``}`` there closed the outer group early,
hiding ``cat f.py`` from the ``} | python3`` it feeds. Now exact words parse whole, and a
``$(…)`` word is unparsable: the guard also walks the script once per later line holding ``$(``.
Every terminator below is the one bash itself uses, checked with a harmless marker.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess

import pytest

from cron import lifecycle_guard_groups
from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import _parse_heredoc_operator

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])

# (delimiter word as written, the body line bash ends the heredoc at): the parser predicts these.
EXACT = [
    ("`echo E`", "`echo E`"),
    ("`echo  E`", "`echo  E`"),
    ("$((1+2))", "$((1+2))"),
    ("$((1 + 2))", "$((1 + 2))"),
    ("${x:-a b}", "${x:-a b}"),
    ("${x:-`echo }`}", "${x:-`echo }`}"),
    ("$'E'", "E"),
    ("$'\\x45'", "E"),
    ('$"E"', "E"),
    ("'$(echo  E)'", "$(echo  E)"),
    ('"\\$(x  y)"', "$(x  y)"),
]
# Bash ends these at its reprint of the `$(…)`, which the parser does not model.
REPRINTED = [
    ("$(echo E)", "$(echo E)"),
    ("$(echo  E)", "$(echo E)"),
    ("$(  echo E )", "$(echo E)"),
    ("$(echo E|cat)", "$(echo E | cat)"),
    ("$(echo E;)", "$(echo E)"),
    ("$(echo E>/dev/null)", "$(echo E > /dev/null)"),
    ("$(<f)", "$(< f)"),
    ("$(echo E&)", "$(echo E &)"),
    ("$([[ a ]])", "$([[ -n a ]])"),
    ("$(echo ')')", "$(echo ')')"),
    ("a$(echo  E)b", "a$(echo E)b"),
    ('"$(echo  E)"', "$(echo E)"),
    ("${x:-$(echo })}", "${x:-$(echo })}"),
    ("${x:-$(echo  E)}", "${x:-$(echo E)}"),
    ("$((1+$(echo  1)))", "$((1+$(echo 1)))"),
]


def _group(delimiter: str, terminator: str, inner: str, sink: str) -> str:
    return f"{{\ncat <<{delimiter}\n}}\n{terminator}\n{inner}\n}} | {sink}"


def _lifecycle_script(tmp_path) -> str:
    script = tmp_path / "f.py"
    script.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    return shlex.quote(str(script))


@pytest.mark.parametrize(("delimiter", "terminator"), EXACT + REPRINTED)
def test_group_feed_after_expansion_delimiter_is_scanned(tmp_path, delimiter, terminator):
    command = _group(delimiter, terminator, f"cat {_lifecycle_script(tmp_path)}", "python3")
    assert scan_gateway_lifecycle(command) == (True, None)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("delimiter", "terminator"), EXACT + REPRINTED)
def test_bash_ends_the_body_at_the_terminator(delimiter, terminator):
    """Bash itself ends the body at ``terminator``: the group's later command reaches the pipe."""
    command = _group(delimiter, terminator, "echo MARKER_FED", "cat")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                            timeout=20, check=False)
    assert "MARKER_FED" in result.stdout.splitlines(), result.stderr


@pytest.mark.parametrize(("delimiter", "terminator"), EXACT)
def test_parser_ends_an_exact_word_where_bash_does(delimiter, terminator):
    parsed = _parse_heredoc_operator(f"<<{delimiter}", 0)
    assert parsed is not None and parsed[1] == terminator


@pytest.mark.parametrize(("delimiter", "_terminator"), REPRINTED)
def test_parser_refuses_a_word_bash_reprints(delimiter, _terminator):
    assert _parse_heredoc_operator(f"<<{delimiter}", 0) is None
    assert _parse_heredoc_operator(f"<<-{delimiter}", 0) is None


@pytest.mark.parametrize(
    ("opener", "delimiter", "quoted"),
    [
        ("<<`echo E`", "`echo E`", False),
        ("<<-`echo E`", "`echo E`", False),
        ("<<${x:-a b}", "${x:-a b}", False),
        ("<<${x:-$((1))}", "${x:-$((1))}", False),
        ("<<$'a\\'b'", "a'b", True),
        ("<<$'\\t'", "\t", True),
        ('<<$"E"', "E", True),
        ("<<E`x`'q'", "E`x`q", True),
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


def test_double_quoted_substitution_body_is_not_masked_past_bash_terminator():
    """``<<"$(echo  E)"`` ends at ``$(echo E)``; masking to the literal word hid what follows."""
    command = f'cat <<"$(echo  E)"\ndata\n$(echo E)\n{LIFECYCLE}\n$(echo  E)'
    assert scan_gateway_lifecycle(command) == (True, None)


def test_reprinted_delimiter_with_harmless_group_stays_allowed():
    command = _group("$(echo  E|cat)", "$(echo E | cat)", "echo fed", "cat")
    assert scan_gateway_lifecycle(command) == (False, None)


def test_later_heredoc_in_a_terminator_view_is_split_from_the_group(tmp_path):
    """The view after a reprinted body still separates a later heredoc's ``}`` line."""
    command = (f"{{\ncat <<$(echo E)\n}}\n$(echo E)\ncat <<X\n}}\nX\n"
               f"cat {_lifecycle_script(tmp_path)}\n}} | python3")
    assert scan_gateway_lifecycle(command) == (True, None)


# (words after the first ``<<``, first body's terminator, second body's terminator): bash reads
# both bodies in order, so the second body's ``}`` line never closes the group.
TWO_WORD_OPENERS = [
    ("$(echo E) <<A", "$(echo E)", "A"),
    ("$(echo  E|cat) <<A", "$(echo E | cat)", "A"),
    ("$(echo E) <<-A", "$(echo E)", "\tA"),
    ("A <<$(echo E)", "A", "$(echo E)"),
    ("$(echo E) <<$(echo F)", "$(echo E)", "$(echo F)"),
    ("$(echo E) <<'$(echo  F)'", "$(echo E)", "$(echo  F)"),
]


def _two_body_group(words: str, first: str, second: str, inner: str, sink: str) -> str:
    return f"{{\ncat <<{words}\nx\n{first}\n}}\n{second}\n{inner}\n}} | {sink}"


@pytest.mark.parametrize(("words", "first", "second"), TWO_WORD_OPENERS)
def test_group_feed_after_two_heredocs_on_one_opener_is_scanned(tmp_path, words, first, second):
    command = _two_body_group(words, first, second, f"cat {_lifecycle_script(tmp_path)}", "python3")
    assert scan_gateway_lifecycle(command) == (True, None)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("words", "first", "second"), TWO_WORD_OPENERS)
def test_bash_reads_both_bodies_of_one_opener(words, first, second):
    command = _two_body_group(words, first, second, "echo MARKER_FED", "cat")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                            timeout=20, check=False)
    assert "MARKER_FED" in result.stdout.splitlines(), result.stderr


def test_two_heredocs_on_one_opener_with_harmless_group_stay_allowed():
    command = _two_body_group("$(echo  E|cat) <<A", "$(echo E | cat)", "A", "echo fed", "cat")
    assert scan_gateway_lifecycle(command) == (False, None)


def _candidate_lines(count: int) -> str:
    lines = [f"echo $(echo {index})" for index in range(count)]
    return "\n".join(["{", "cat <<$(echo E)", *lines, "} | cat"])


def test_terminator_views_up_to_the_cap_are_walked():
    cap = lifecycle_guard_groups.MAX_REPRINTED_TERMINATOR_VIEWS
    assert not lifecycle_guard_groups.heredoc_views_overflow(_candidate_lines(cap))
    assert scan_gateway_lifecycle(_candidate_lines(cap)) == (False, None)


def test_terminator_views_past_the_cap_fail_closed():
    cap = lifecycle_guard_groups.MAX_REPRINTED_TERMINATOR_VIEWS
    command = _candidate_lines(cap + 1)
    assert lifecycle_guard_groups.heredoc_views_overflow(command)
    unsafe, refusal = scan_gateway_lifecycle(command)
    assert unsafe and refusal and "plain word" in refusal
