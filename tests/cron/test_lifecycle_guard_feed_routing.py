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
        # Literal private-use words must not turn into pipeline-ending ampersands.
        "cat {py} \ue000 | python3",
        "cat {py} \ue000\ue000 | python3",
        "cat {py} \ue000 2>&1 | python3",
        "cat {py} \ue000\ue001 2>&1 | python3",
        # 2. Output process substitution, including redirects after the body.
        "cat > >(python3) < {py}",
        "cat > >(python3) <{py}",
        "cat >> >(python3) < {py}",
        "cat > >(python3) < {py} 2>&1",
        "cat > >(python3) > >(wc -l) < {py}",
        "cat {py} > >(python3)",
        "cat {sh} > >(bash)",
        "cat {py} | tee >(python3)",
        # The `>(…)` body reads the whole command's output, words after it included.
        "cat > >(python3) {py}",
        "cat > >(bash) {sh}",
        "cat >> >(python3) {py}",
        "cat 1> >(python3) < {py}",
        "tee >(python3) < {py}",
        "tee < {py} >(python3)",
        "{{ tee >(python3); }} < {py}",
        "{{ cat > >(python3); }} < {py}",
        "{{ bash <(cat); }} < {sh}",
        "cat {clean} | tee {out} >(python3) < {py}",
        "tee {out} < {py} | python3",
        # `>|` is one redirection operator, not `>` and a pipe.
        "cat {py} >| >(python3)",
        "cat >| >(bash) {sh}",
        "cat {py} &> >(python3)",
        # A `>(…)` body writes where its command's stdout goes; nested bodies read on.
        "echo >(cat {py}) | python3",
        "tee >(python3) >(cat) < {py}",
        "cat {py} > >(cat | python3)",
        "cat {py} > >(cat > >(python3))",
        "cat > >(cat <(cat) > >(python3)) {py}",
        "{{ cat <(cat) > >(python3); }} < {py}",
        # 3. A `cat`/`tee` with no file operand passes its stdin through.
        "cat {py} | cat | python3",
        "cat {py} | cat - | python3",
        "cat {py} | if true; then cat; fi | python3",
        "cat {py} | {{ cat; }} | python3",
        "cat {py} | tee /dev/null | python3",
        "cat {py} | tee /dev/null 2<{clean} | python3",
        "cat {py} | cat 2<{clean} | python3",
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
        # A `&>` glued to the word before it is still a redirection, not part of that word.
        "bash {sh}&>/dev/null",
        "bash {sh}&>/dev/null &",
        "bash {sh}&>>{out}",
        ". {sh}&>/dev/null",
        "python3 {py}&>/dev/null",
        "python3 {py}&>{out}",
        "python3 <{py}&>/dev/null",
        "python3&>/dev/null <{py}",
        "cat {py}|python3&>/dev/null",
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
        "cat {py} | tee /dev/null < {clean} | python3",
        "cat {py} | true | python3",
        # A closed subshell's output ends at the next operator.
        "(cat {py}); echo hi | python3",
        "(cat {py}) > {out}; echo hi | python3",
        # Only a trailing pipe continues a line.
        "cat {py};\npython3",
        "cat {py} &&\npython3",
        "cat {py}\npython3",
        "echo '>&' | python3 {clean}",
        # `>|f` clobbers f; it pipes nothing on.
        "cat {py} >|{out}; python3 {clean}",
        # A `>(…)` body fed by a `cat` reading another file.
        "cat > >(python3) {clean}",
    ],
)
def test_routing_mentions_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


@pytest.mark.parametrize("group", ["{{ cat {source}; }}", "(cat {source})",
                                    "if true; then cat {source}; fi"])
@pytest.mark.parametrize("suffix", ["> >(python3)", "2>&1 > >(python3)",
                                     "> >(cat > >(python3))",
                                     "> >(cat) | python3", "> >(cat) > >(python3)"])
@pytest.mark.parametrize("lifecycle", [True, False])
def test_closed_group_feeds_output_substitution(files, group, suffix, lifecycle):
    source = files["py" if lifecycle else "clean"]
    command = group.format(source=source) + " " + suffix
    assert scan_gateway_lifecycle(command) == (lifecycle, None)


@pytest.mark.parametrize("group", ["{ true; }", "(true)", "if true; then true; fi"])
@pytest.mark.parametrize("lifecycle", [True, False])
def test_substitution_output_survives_closed_group(files, group, lifecycle):
    source = files["py" if lifecycle else "clean"]
    command = group + f" > >(cat {source}) | python3"
    assert scan_gateway_lifecycle(command) == (lifecycle, None)


@pytest.mark.parametrize("group", ["{{ cat; }}", "(cat)", "if true; then cat; fi"])
def test_closed_group_redirected_stdin_survives_substitution(files, group):
    command = group.format() + " > >(python3) < " + files["py"]
    assert scan_gateway_lifecycle(command) == (True, None)


@pytest.mark.parametrize("group", ["{{ cat {py}; }}", "(cat {py})"])
def test_closed_group_does_not_leak_into_substitution_pipeline(files, group):
    command = group.format(**files) + " > >(cat {clean} | python3)".format(**files)
    assert scan_gateway_lifecycle(command) == (False, None)


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
    # The `|` of `>|` (clobber) stays in its redirection; `> |` is still a pipe.
    assert words("a >|x 2>| y | b") == ["a", ">|x", "2>|", "y", "|", "b"]
    assert words("a > | b '>|'") == ["a", ">", "|", "b", ">|"]
    assert words("a \ue001 b") == ["a", "\ue001", "b"]


def test_glued_ampersand_redirection_splits_from_its_word():
    from cron.lifecycle_guard_groups import split_glued_redirections as split

    assert split("python3 f.py&>x") == r"python3 f.py \&>x"
    assert split("a 'b'&>>y") == r"a 'b' \&>>y"
    # Main's fd normalizer escapes duplication punctuation for shlex; it stays one token.
    assert split("a 2>&1 >&2") == r"a 2>\&1 >\&2"
    # Control operators, quoted ampersands and lone `&` remain unchanged. An escaped `&`
    # belongs to the word, so only the `>` after it splits off.
    assert split("a&&>x a|&>x a & b") == "a&&>x a|&>x a & b"
    assert split("echo 'a&>x' a\\&>x") == "echo 'a&>x' a\\& >x"
