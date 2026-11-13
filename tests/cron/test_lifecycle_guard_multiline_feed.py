"""Stdin feeds lost through a compound command spanning lines (PR #2485 follow-up).

Each logical line used to be walked alone, so a group or compound command whose body spans
lines lost its feed: ``{`` newline ``cat f.py`` newline ``} | python3`` ran a lifecycle ``.py``
the guard never opened. The whole command is now one walk, a newline ending a command as ``;``
does. A heredoc body is walked as its own script, so a ``{``/``}`` line in it never opens or
closes a group of the command around it.
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
        # The card's four cases.
        "{{\ncat {py}\n}} | python3",
        "cat {py} | while read l; do\npython3\ndone",
        "cat {py} | if true; then\npython3\nfi",
        "(\ncat {py}\n) | python3",
        # The same shapes spelled other ways.
        "{{\n  cat {py}\n}} 2>&1 | python3",
        "{{ cat {py}\n}} | python3",
        "{{\ncat {py}; }} | python3",
        "{{\necho start\ncat {py}\necho end\n}} | python3",
        "{{\n\ncat {py}\n\n}} | python3",
        "{{  # header\ncat {py}  # the program\n}} | python3",
        "cat {py} | while read l\ndo\npython3\ndone",
        "cat {py} | until false\ndo\npython3\ndone",
        "cat {py} | for i in 1; do\npython3\ndone",
        "cat {py} | if true\nthen\npython3\nfi",
        "cat {py} | if false; then\ntrue\nelse\npython3\nfi",
        "cat {py} | case x in\nx) python3 ;;\nesac",
        "cat {py} | {{\npython3\n}}",
        "cat {py} | (\npython3\n)",
        "cat {sh} | {{\nbash\n}}",
        "{{\ncat {sh}\n}} | bash",
        "(cat {py}\n) | python3",
        # Nested compounds across lines.
        "{{\nif true; then\ncat {py}\nfi\n}} | python3",
        "cat {py} | (\nif true; then\npython3\nfi\n)",
        "(\n(\ncat {py}\n)\n) | python3",
        # Pipe continuations still join, inside a multi-line group.
        "{{\ncat {py} |\ncat\n}} | python3",
        # A process substitution spanning lines.
        "python3 <(\ncat {py}\n)",
        "bash <(\ncat {sh}\n)",
        # A quoted newline inside the group body is data, not a line break.
        "{{\necho 'a\nb'\ncat {py}\n}} | python3",
        # A heredoc body's `}` does not close the group around its command.
        "{{\ncat <<EOF\n}}\nEOF\ncat {py}\n}} | python3",
        "{{\npython3 - <<'PY'\nif True:\n    pass\nPY\ncat {py}\n}} | python3",
        # A shell's heredoc body is a script of its own, walked as one.
        "bash <<EOF\n{{\ncat {py}\n}} | python3\nEOF",
        "bash <<'EOF'\ncat {py} | while read l; do\npython3\ndone\nEOF",
    ],
)
def test_multiline_compound_feeds_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # The card's allowed case: the group's output ends at `;`.
        "{{\ncat {py}\n}}; python3 {clean}",
        "{{\ncat {py}\n}}; python3",
        "{{\ncat {py}\n}}\npython3",
        "(\ncat {py}\n)\npython3",
        "(\ncat {py}\n) > {out}\npython3",
        "if true; then\ncat {py}\nfi\npython3",
        # Multi-line bodies that never feed an interpreter.
        "cat {py} | while read l; do\necho \"$l\"\ndone",
        "cat {py} | while read l; do\necho x\ndone\npython3",
        "cat {py} | if true; then\nwc -l\nfi\npython3",
        "{{\ncat {py}\n}} | wc -l\npython3",
        "{{\ncat {py}\n}} > {out}\npython3 {clean}",
        "cat {py} | {{\nwc -l\n}}\npython3",
        # A newline ends a command as `;` does: the next line reads no pipe.
        "cat {py}\npython3",
        "{{\ncat {py}\npython3\n}}",
        "(\ncat {py}\npython3\n)",
        # A heredoc body's `{` does not open a group the command's `}` then closes.
        "cat <<EOF\n{{\nEOF\ncat {py}\n}} | python3",
        "cat <<EOF\n(\nEOF\ncat {py}\n) | python3",
    ],
)
def test_multiline_bodies_without_a_feed_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


def test_heredoc_scripts_walk_each_body_alone():
    from cron.lifecycle_guard_groups import heredoc_scripts

    def scripts(command: str) -> list[str]:
        return list(heredoc_scripts(command))

    assert scripts("a\nb") == ["a\nb"]
    assert scripts("cat <<EOF\n{\nEOF\n}") == ["cat <<EOF\n}", "{\n"]
    # Two bodies on one opener line, read in order (the last body is walked first).
    assert scripts("cat <<A <<'B'\n1\nA\n2\nB\nx") == ["cat <<A <<'B'\nx", "2\n", "1\n"]
    assert scripts("cat <<-A\n\t1\n\tA\nx") == ["cat <<-A\nx", "\t1\n"]
    # A heredoc nested in a shell's body is a script of its own as well.
    assert scripts("bash <<'O'\ncat <<I\n}\nI\nO\nx") == ["bash <<'O'\nx", "cat <<I\n", "}\n"]
    # An unterminated body, a here-string, a quoted `<<` or an opener without a word splits
    # nothing from there on (the shell reads such text as commands, or fails).
    assert scripts("cat <<EOF\n{\n") == ["cat <<EOF\n{\n"]
    assert scripts("cat <<A\n1\nA\ncat <<B\n{") == ["cat <<A\ncat <<B\n{", "1\n"]
    assert scripts("cat <<< x\n{\nx") == ["cat <<< x\n{\nx"]
    assert scripts("echo '<<x'\n{\nx") == ["echo '<<x'\n{\nx"]
    assert scripts("cat <<\n{\n") == ["cat <<\n{\n"]
    assert scripts("cat <<\ncat <<A\n1\nA") == ["cat <<\ncat <<A\n1\nA"]
    # A body left unterminated runs to the end, so no later opener splits one off.
    assert scripts("cat <<B\n{\ncat <<A\n1\nA") == ["cat <<B\n{\ncat <<A\n1\nA"]
    assert scripts("cat <<''") == ["cat <<''"]
    assert scripts("cat <<A\nA") == ["cat <<A\n", ""]
