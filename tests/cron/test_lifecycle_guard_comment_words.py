"""A ``#`` starts a comment only at the start of a word (card t_01a1d6e0355ac8cc).

bash reads ``a#b`` as one word, but the guard's shlex tokenizer treated every unquoted ``#`` as a
comment and dropped the rest of the line: ``echo a#b; bash ./r.sh`` ran the script unread. And
the logical-line splitter ignored comments, so an apostrophe in one (``echo hi # it's``) opened
a "quote" that swallowed the next lines into one line, which the tokenizer then cut at that
comment. Real bash runs each RUN row with a fake ``hermes`` that writes a marker.
"""

from __future__ import annotations

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_lines import (
    _split_logical_lines, join_line_continuations, strip_shell_comments,
)

RUN = [
    # Escaped separators and a bare CR are word content, not comment boundaries.
    "echo a\\ #b; bash ./r.sh",
    "echo a\\;#b; bash ./r.sh",
    "echo a\\|#b; bash ./r.sh",
    "echo a\\<#b; bash ./r.sh",
    "echo a\r#b; bash ./r.sh",
    # A `#` inside a word is text.
    "echo a#b; bash ./r.sh",
    "echo a#b && bash ./r.sh",
    "echo a#b | bash ./r.sh",
    "x=1#2 bash ./r.sh",
    "echo a#b c#d; bash -s < ./r.sh",
    "echo $#; bash ./r.sh",
    "echo a#b; cat ./r.sh | bash",
    "echo a#b; hermes gateway \"restart\"",
    "echo a#b; hermes gateway re\"start\"",
    "echo a#b 2>/dev/null; bash ./r.sh",
    "x=1#2 {fd}>/dev/null bash ./r.sh",
    "echo a#b {fd}>/dev/null; bash ./r.sh",
    # A quote inside a comment is text: the next line is its own command.
    "echo hi # it's\nbash -s -<./r.sh",
    "echo # it's\nbash<./r.sh",
    "true # it's\nbash ./r.sh 2>&1",
    "echo hi # it's\nbash ./r.sh",
    "echo hi #it's\nbash ./r.sh",
    "echo hi;# it's\nbash ./r.sh",
    "# it's\nbash ./r.sh",
    "echo hi # say \"x\nbash ./r.sh",
    "echo hi # it's\necho ok\nbash ./r.sh",
    "echo a#b # it's\nbash ./r.sh",
    # A single-quoted backslash escapes nothing: the quote closes and the next line runs.
    "echo 'a\\'\nbash ./r.sh",
    # A comment ends at its newline even after a backslash; a continuation glued to `#` joins
    # the word (`a\<newline>#b` is `a#b`).
    "echo hi # x \\\nbash ./r.sh",
    "true # x \\\nhermes gateway \"restart\"",
    "grep x /dev/null # x \\\nhermes gateway \"restart\"",
    "echo a\\\n#b; bash ./r.sh",
    "grep x /dev/null a\\\n#b; hermes gateway \"restart\"",
]
ALLOWED = [
    "echo a\\ #b; python3 ./clean.py",
    "echo a\\;#b; python3 ./clean.py",
    "echo a\\|#b; python3 ./clean.py",
    "echo a\\<#b; python3 ./clean.py",
    "echo a\r#b; python3 ./clean.py",
    "x=1#2 {fd}>/dev/null python3 ./clean.py",
    "echo a#b {fd}>/dev/null; python3 ./clean.py",
    "echo ''#b; python3 ./clean.py",
    "echo a\\ #b # bash ./r.sh",
    "echo a#b; python3 ./clean.py",
    "x=1#2 python3 ./clean.py",
    "echo hi # it's\npython3 ./clean.py",
    "echo # it's\npython3<./clean.py",
    # A word-start `#` still comments out the rest of its line.
    "echo a#b # bash ./r.sh",
    "echo hi # it's bash ./r.sh",
    "echo hi;# bash ./r.sh",
    "echo hi # x\n# bash ./r.sh",
    # A quoted or escaped `#` is text, and so is what follows it.
    "echo 'a # b'; python3 ./clean.py",
    "echo \\#a; python3 ./clean.py",
    "echo a \\\n#b; bash ./r.sh",
]


def test_comment_quote_does_not_break_data_sink_exemption():
    # Data-sink masking tokenizes the original command, before logical-line splitting.
    assert scan_gateway_lifecycle("grep 'hermes gateway restart' /dev/null # it's") == (False, None)


def test_comment_quote_does_not_hide_direct_lifecycle_command():
    assert scan_gateway_lifecycle("hermes gateway restart # it's")[0] is True


def _tree(tmp_path):
    (tmp_path / "r.sh").write_text('hermes gateway "restart"\n')
    (tmp_path / "clean.py").write_text("print(1)\n")
    (tmp_path / "bin").mkdir()
    tool = tmp_path / "bin" / "hermes"
    tool.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    tool.chmod(0o755)


def _bash_runs(tmp_path, command: str) -> bool:
    marker = tmp_path / "marker"
    marker.unlink(missing_ok=True)
    subprocess.run(["/bin/bash"], input=command + "\n", text=True, capture_output=True,
                   timeout=20, check=False, cwd=tmp_path,
                   env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "MARKER": str(marker),
                        "HOME": str(tmp_path)})
    return marker.exists()


@pytest.mark.parametrize("command", RUN)
def test_command_after_a_mid_word_hash_or_quoted_comment_is_refused(tmp_path, command):
    _tree(tmp_path)
    assert _bash_runs(tmp_path, command), "bash must run the restart, or the row proves nothing"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


@pytest.mark.parametrize("command", ALLOWED)
def test_clean_twins_and_real_comments_stay_allowed(tmp_path, command):
    _tree(tmp_path)
    assert not _bash_runs(tmp_path, command)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize(("text", "lines"), [
    ("echo hi # it's\nbash x", ["echo hi ", "bash x"]),
    ("echo a#b; c", ["echo a#b; c"]),
    ("echo 'a\n#b'\nc", ["echo 'a\n#b'", "c"]),
    ("echo \"it's # x\"\nc", ["echo \"it's # x\"", "c"]),
    ("echo \\#'\nc'", ["echo \\#'\nc'"]),
    ("a;#'\nb", ["a;", "b"]),
    ("a|#'\nb", ["a|", "b"]),
    ("x=#'\nb'", ["x=#'\nb'"]),
])
def test_split_logical_lines_skips_comments(text, lines):
    assert _split_logical_lines(text) == lines


@pytest.mark.parametrize(("line", "stripped"), [
    ("echo a#b; c", "echo a#b; c"),
    ("echo a # b; c", "echo a "),
    ("echo a;#b\nc", "echo a;\nc"),
    ("echo >#x", "echo >"),
    ("echo '#' \"#\" \\# ${#x} $# x#", "echo '#' \"#\" \\# ${#x} $# x#"),
    ("#all", ""),
    ("  # x 'y\nz", "  \nz"),
])
def test_strip_shell_comments_only_at_word_start(line, stripped):
    assert strip_shell_comments(line) == stripped


@pytest.mark.parametrize(("text", "joined"), [
    ("a \\\n  b", "a  b"),
    ("a # x \\\nb", "a # x \nb"),
    ("a # x \\\\\nb", "a # x \nb"),
    ("a\\\n#b", "a#b"),
    ("a \\\n#b", "a #b"),
    ("a\\\n  #b", "a #b"),
    ("echo '#' \\\nb", "echo '#'  b"),
])
def test_join_line_continuations_ends_comments_at_their_newline(text, joined):
    assert join_line_continuations(text) == joined
