"""Stdin feeds through shell compound commands (PR #2316 review 5893771725).

Each case below ran a lifecycle ``.py``/``.sh`` the guard never opened:

1. A brace group's output piped on (``{ cat f.py; } | python3``): at the ``|`` the previous
   segment is ``}``, which is no ``cat``. A group writes what each of its commands writes, unless
   that command's output is itself piped elsewhere inside the group.
2. An unquoted ``<`` glued to an argument (``python3 -<f.py``) redirects stdin. Quoted text
   (``bash -c 'python3<f.py'``) is still one word, re-scanned whole as a payload.
3. A pipe into ``if``/``while``/``until``/``for``/``case`` feeds every command in the body.
4. Process substitution as the program file or stdin (``python3 <(cat f.py)``,
   ``bash < <(cat f.sh)``).
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
    clean = tmp_path / "clean.py"
    clean.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    clean_sh = tmp_path / "clean.sh"
    clean_sh.write_text("echo ok\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean": clean, "clean_sh": clean_sh}.items()}


@pytest.mark.parametrize(
    "template",
    [
        # 1. A group's output piped on.
        "{{ cat {py}; }} | python3",
        "{{ cat {py}; true; }} | python3",
        "{{ true; cat {py}; }} | python3",
        "(cat {py}; true) | python3",
        "{{ {{ cat {py}; }}; }} | python3",
        "{{ cat {sh}; }} | bash",
        "if true; then cat {py}; fi | python3",
        # 2. An unquoted `<` glued to an argument.
        "python3 -<{py}",
        "python3 -u<{py}",
        "bash -s<{sh}",
        "bash -<{sh}",
        'python3 "-"<{py}',
        # 3. A pipe into a compound command feeds its whole body.
        "cat {py} | if true; then python3; fi",
        "cat {py} | if false; then :; else python3; fi",
        "cat {py} | if python3; then :; fi",
        "cat {py} | while true; do python3; done",
        "cat {py} | until false; do python3; done",
        "cat {py} | for x in 1; do python3; done",
        "cat {sh} | if true; then bash; fi",
        "cat {py} | case x in *) python3;; esac",
        "cat {py} | (if true; then python3; fi)",
        "cat {py} | while true; do if true; then python3; fi; done",
        # 4. Process substitution as the program file or stdin.
        "python3 <(cat {py})",
        "python3<(cat {py})",
        "python3 -u <(cat {py})",
        "python3 < <(cat {py})",
        "bash <(cat {sh})",
        "bash < <(cat {sh})",
        "source <(cat {sh})",
        "python3 <(cat {py}) <(cat {clean})",
        "python3 <({{ cat {py}; }})",
    ],
)
def test_compound_stdin_feeds_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # A command whose output is piped elsewhere inside the group writes nothing out of it.
        "{{ cat {py} | grep x; }} | python3",
        "(cat {py} | grep x) | python3",
        "{{ cat {py}; }}; python3",
        "{{ cat {py}; }} > /dev/null; python3 {clean}",
        # Quoted `<` is data; a fd number before `>` is part of the redirection.
        "echo 'python3 -<{py}'",
        'echo "a<{py}"',
        "echo a\\<{py}",
        "python3 {clean} 2>{py}",
        "grep -c '<' {py}",
        "bash -c 'cat<{sh}'",
        # The pipe's feed ends with the compound command.
        "cat {py} | if true; then :; fi; python3",
        "cat {py} | while false; do :; done; python3 {clean}",
        "cat {py} | if true; then python3 {clean}; fi",
        "cat {sh} | case x in *) grep x;; esac; bash {clean_sh}",
        # `fi`/`done` are words, not closers, outside command position.
        "cat {py} | if true; then echo fi; python3 {clean}; fi",
        # A process substitution that writes no file runs nothing the scan can open.
        "python3 <(echo hi)",
        "diff <(cat {py}) <(cat {clean})",
        "python3 {clean} <(cat {py})",
        "python3 <(cat {clean}) <(cat {py})",
        "cat {py} | python3 <(echo hi)",
    ],
)
def test_compound_mentions_and_data_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


def test_split_glued_redirections_is_quote_aware():
    from cron.lifecycle_guard_groups import split_glued_redirections as split

    assert split("python3 -<f.py") == "python3 - <f.py"
    assert split("python3<(cat f.py)") == "python3 <(cat f.py)"
    assert split("cat a<b<c") == "cat a <b <c"
    # Descriptor numbers and the operator's own second character stay glued.
    assert split("x 2>err 0<in") == "x 2>err 0<in"
    assert split("cat<<EOF a>>log b<>rw") == "cat <<EOF a >>log b <>rw"
    # Quoted or escaped `<` is text.
    assert split("bash -c 'python3<f.py'") == "bash -c 'python3<f.py'"
    assert split('echo "a<b" a\\<b') == 'echo "a<b" a\\<b'
    assert split("x 'q'<f") == "x 'q' <f"
