"""A redirection's ``&`` is never a background operator (t_01a1d655f3e69b6e).

The tokenizer split ``2>&1`` into ``2>``, ``&``, ``1``: the lone ``&`` was walked as a control
operator, so ``cat f.sh 2>&1 | bash`` became ``cat f.sh 2>`` (backgrounded, its output routed
nowhere) and ``1 | bash``, and the pipe fed ``bash`` nothing. The same split hid ``>&2``,
``<&0``, ``>&f``, ``&>f`` and ``&>>f``, and let the data-sink exemption read ``env >& grep
<lifecycle words>`` as a ``grep`` whose pattern is data, while ``env`` runs those words.

``bash 3<f.sh <&3`` also ran ``f.sh`` unscanned: a ``<&N`` duplicating a descriptor opened on a
file in the same command reads that file.
"""

from __future__ import annotations

import shlex

import pytest

from cron.lifecycle_guard import _iter_command_token_lists, scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])


@pytest.fixture
def files(tmp_path) -> dict[str, str]:
    sh = tmp_path / "r.sh"
    sh.write_text(f"#!/bin/sh\necho hi\n{LIFECYCLE}\n", encoding="utf-8")
    py = tmp_path / "r.py"
    py.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    clean_sh = tmp_path / "clean.sh"
    clean_sh.write_text("echo ok\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean_sh": clean_sh}.items()}


@pytest.mark.parametrize(
    "template",
    [
        # The card's three forms.
        "cat {sh} 2>&1 | bash",
        "{{ cat {sh}; }} 2>&1 | bash",
        "(cat {sh}) 2>&1 | bash",
        # Other descriptor duplications on the writer.
        "cat {sh} 2>& 1 | bash",
        "cat {sh} 2>&1|bash",
        "cat {sh} 2>&- | bash",
        "cat {sh} 1>&1 | bash",
        "cat <&0 {sh} | bash",
        "cat {sh} 2>&1 | cat 2>&1 | bash",
        "cat {py} 2>&1 | python3",
        # A duplication on the reader no longer drops its own stdin or script operand.
        "bash 2>&1 < {sh}",
        "bash 2>&1 {sh}",
        "bash >&2 {sh}",
        "bash &>/dev/null {sh}",
        "bash &>>/tmp/log {sh}",
        "bash&>/dev/null {sh}",
        "python3 2>&1 {py}",
        # `<&N` reads the file `N<f` opened in the same command.
        "bash 3<{sh} <&3",
        "bash 3< {sh} 0<& 3",
        "{{ bash; }} 3<{sh} <&3",
        "cat 3<{sh} <&3 | bash",
    ],
)
def test_descriptor_duplication_keeps_the_command_whole(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "command",
    [
        # `env` runs the words after the redirection target; they are not grep's data.
        f"env >& grep {LIFECYCLE}",
        f"env >&grep {LIFECYCLE}",
        f"nohup 2>& rg {LIFECYCLE}",
        f"env &> grep {LIFECYCLE}",
    ],
)
def test_redirection_target_is_not_a_data_sink(command):
    assert scan_gateway_lifecycle(command) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # A real background `&` still splits: `cat` writes to the terminal, `bash` reads nothing.
        "cat {sh} & bash",
        "cat {sh} &bash",
        # A clean script stays clean however its output is duplicated.
        "cat {clean_sh} 2>&1 | bash",
        "bash 2>&1 {clean_sh}",
        "bash 3<{clean_sh} <&3",
        # `<&N` on a descriptor no file opened here reads no known file.
        "bash 3<{sh} <&4",
        "bash 3<{sh}",
    ],
)
def test_background_and_clean_forms_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


@pytest.mark.parametrize(
    "command",
    [
        # Mentions and data: the duplication must not turn them into commands.
        f"grep -rn '{LIFECYCLE}' docs 2>&1 | head",
        f"grep '{LIFECYCLE}' log &>/dev/null",
        f"rg '{LIFECYCLE}' 2>&1",
        "echo '2>&1' | bash",
    ],
)
def test_mentions_and_data_stay_allowed(command):
    assert scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize(
    ("command", "tokens"),
    [
        ("(cat x) 2>&1 | bash", ["(", "cat", "x", ")", "2>&1", "|", "bash"]),
        ("a 2>& 1", ["a", "2>&", "1"]),
        ("a >&2 <&0 >&-", ["a", ">&2", "<&0", ">&-"]),
        ("a &>f &>>g", ["a", "&>f", "&>>g"]),
        ("a&>f", ["a", "&>f"]),
        ("a 2&>f", ["a", "2", "&>f"]),
        # Control operators keep their `&`.
        ("a & b", ["a", "&", "b"]),
        ("a&&>f b", ["a", "&&", ">f", "b"]),
        ("a |&>f b", ["a", "|&", ">f", "b"]),
        ("a >&2 & b", ["a", ">&2", "&", "b"]),
        # A quoted or escaped `>` is text, so the `&` after it is a control operator.
        ("a '>'& b", ["a", ">", "&", "b"]),
        ("a \\>& b", ["a", ">", "&", "b"]),
        ("echo '2>&1' x", ["echo", "2>&1", "x"]),
    ],
)
def test_redirection_ampersand_tokenization(command, tokens):
    assert list(_iter_command_token_lists(command)) == [tokens]
