"""Stdin feeds lost between a ``cat`` and the program reading it (PR #2393 review).

Each case below ran a lifecycle ``.py``/``.sh`` the guard never opened:

1. A descriptor duplication on the feeding command (``cat f.py 2>&1 | python3``): the tokenizer
   split ``2>&1`` into ``2>``, ``&``, ``1`` and the walk read the ``&`` as the background
   operator, so the feed became ``cat_operands(['1'])``. ``&>file`` split the same way.
2. Output process substitution (``cat f.py > >(python3)``): the ``>(…)`` body read nothing.
3. A ``cat`` (or ``tee``) with no file operand passes its stdin through
   (``cat f.py | cat | python3``).
4. A pipe continued on the next line (``cat f.py |`` newline ``python3``): each line was walked
   alone.
"""

from __future__ import annotations

import shlex

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])


@pytest.fixture
def files(tmp_path) -> dict[str, str]:
    sh = tmp_path / "restart.sh"
    sh.write_text(f"#!/bin/sh\necho hi\n{LIFECYCLE}\n", encoding="utf-8")
    py = tmp_path / "f.py"
    py.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    clean = tmp_path / "c.py"
    clean.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    return {"sh": shlex.quote(str(sh)), "py": shlex.quote(str(py)),
            "clean": shlex.quote(str(clean)), "out": shlex.quote(str(tmp_path / "out"))}


@pytest.mark.parametrize(
    "template",
    [
        # 1. Descriptor duplication on the feeding command or group.
        "cat {py} 2>&1 | python3",
        "cat {py} 2>&1|python3",
        "{{ cat {py}; }} 2>&1 | python3",
        "{{ cat {py}; }} >&2 | python3",
        "(cat {py}) 2>&1 | python3",
        "(cat {py}) | python3",
        "if true; then cat {py}; fi 2>&1 | python3",
        "cat {sh} 2>&1 | bash",
        "cat {py} 2>&1 |& python3",
        "cat {py} 2>/dev/null 2>&1 | python3",
        # 2. Output process substitution.
        "cat {py} > >(python3)",
        "cat {sh} > >(bash)",
        "cat {py} | tee >(python3)",
        # 3. A `cat`/`tee` with no file operand passes its stdin through.
        "cat {py} | cat | python3",
        "cat {py} | cat - | python3",
        "cat {py} | if true; then cat; fi | python3",
        "cat {py} | {{ cat; }} | python3",
        "cat {py} | tee /dev/null | python3",
        "cat {py} | cat | cat | python3",
        # 4. A pipe continued on the next line.
        "cat {py} |\npython3",
        "cat {py} |\nif true; then python3; fi",
        "cat {py} |\n\npython3",
        "cat {py} | # comment\npython3",
        "cat {py} |&\npython3",
        "(cat {py})|\npython3",
        "{{ cat {py} &&\ntrue; }} | python3",
        "{{ false ||\ncat {py}; }} | python3",
        "python3 {py} |",
        # The program reader's own `&>`/`>&` redirections leave it reading the pipe.
        "cat {py} | python3 &>{out}",
        "cat {py} | python3 2>&1",
        # A `cat` reading the pipe inside `<(…)`, and beside a file.
        "cat {sh} | bash <(cat)",
        "cat {py} | cat {clean} - | python3",
    ],
)
def test_routed_stdin_feeds_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        "cat {py} &>/dev/null | python3 {clean}",
        "cat {py} &>>{out} | python3 {clean}",
        "cat {py} 2>&1 | wc -l",
        "cat {py} > {out}; python3 {clean}",
        # A real background `&` still ends the feed, beside a duplication.
        "cat {py} 2>&1 & python3",
        "cat {py} & python3",
        # A `cat` reading a file (or its own redirected stdin) does not pass the pipe through.
        "cat {py} | cat {clean} | python3",
        "cat {py} | cat < {clean} | python3",
        "cat | python3",
        # `tee`'s operands are files it writes, not reads.
        "cat {clean} | tee {py} | python3",
        # A closed subshell's output ends at the next operator.
        "(cat {py}); echo hi | python3",
        "(cat {py}) > {out}; echo hi | python3",
        # Only a trailing pipe continues a line.
        "cat {py};\npython3",
        "cat {py} &&\npython3",
        "cat {py}\npython3",
        "echo '>&' | python3 {clean}",
    ],
)
def test_routing_mentions_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


def test_redirection_ampersands_survive_tokenizing():
    from cron.lifecycle_guard_groups import protect_redirection_ampersands as protect
    from cron.lifecycle_guard_groups import restore_redirection_ampersands as restore

    def words(line: str) -> list[str]:
        lexer = shlex.shlex(protect(line), posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        return [restore(token) for token in lexer]

    assert words("a 2>&1 >&2 <&0 &>x &>>y b") == ["a", "2>&1", ">&2", "<&0", "&>x", "&>>y", "b"]
    # Control operators keep their `&`.
    assert words("a & b && c |& d") == ["a", "&", "b", "&&", "c", "|&", "d"]
    assert words("a&&>x") == ["a", "&&", ">x"]
    assert words("a|&>x") == ["a", "|&", ">x"]
    # Quoted or escaped text is not a redirection, and the scan leaves it as written.
    assert words("echo '2>&1' \"&>\" 'a&'>x") == ["echo", "2>&1", "&>", "a&>x"]
    assert words("echo \\>&1") == ["echo", ">", "&", "1"]
    assert words("echo '>'&1") == ["echo", ">", "&", "1"]
