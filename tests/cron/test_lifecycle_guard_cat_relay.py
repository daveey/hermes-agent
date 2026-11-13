"""A bare ``cat`` relays the stdin it is fed (t_01a1d5c442ed5fc9, PR #2442 review).

``cat`` with no file operand, or with a ``-`` one, writes its own stdin. The guard named only a
``cat``'s operands and its own ``<`` redirections, never the feed a pipe or an enclosing group
gives it, so ``cat f.sh | cat | bash`` and ``{ cat; } < f.sh | bash`` ran a lifecycle script the
guard never opened. A ``cat`` that reads a file operand (``cat x.txt``) or its own redirection,
heredoc or here-string ignores the feed.

A subshell's own redirection (``(cat f.sh) 2>/dev/null | bash``, ``(cat) < f.sh | bash``) also
dropped what the subshell wrote: the words after ``)`` flushed as a segment that wrote nothing.
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
    clean_sh = tmp_path / "clean.sh"
    clean_sh.write_text("echo ok\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean_sh": clean_sh}.items()}


@pytest.mark.parametrize(
    "template",
    [
        # A pipe feeds a bare `cat`.
        "cat {sh} | cat | bash",
        "cat {sh} | cat | cat | bash",
        "cat {sh} | cat - | bash",
        "cat {sh} | cat -u | bash",
        "cat {sh} | cat - {clean_sh} | bash",
        "cat {sh} | cat {clean_sh} - | bash",
        "cat {sh} | cat 2>/dev/null | bash",
        "cat {sh} | /bin/cat | bash",
        "cat {py} | cat | python3",
        "cat {sh} | (cat) | bash",
        "cat {sh} | {{ cat; }} | bash",
        "cat {sh} | {{ true; cat; }} | bash",
        "bash < <(cat {sh} | cat)",
        # A group's own redirection feeds a bare `cat` inside it.
        "{{ cat; }} < {sh} | bash",
        "{{ cat -; }} < {sh} | bash",
        "(cat) < {sh} | bash",
        "( cat ) <{sh} | bash",
        "{{ cat; }} < {py} | python3",
        # A file redirected to the pipeline's first `cat` still flows through the next one.
        "cat < {sh} | cat | bash",
        # A subshell's own redirection keeps what the subshell wrote (it was dropped).
        "(cat {sh}) 2>/dev/null | bash",
        "(cat {sh}) < /dev/null | bash",
        "( {{ cat {sh}; }} ) 2>/dev/null | bash",
    ],
)
def test_bare_cat_relays_its_feed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # A file operand without `-`: the feed is ignored.
        "cat {sh} | cat {clean_sh} | bash",
        "cat {sh} | cat -- {clean_sh} | bash",
        # Its own redirection, heredoc or here-string replaces the feed.
        "cat {sh} | cat < {clean_sh} | bash",
        "cat {sh} | cat <<< 'echo ok' | bash",
        "cat {sh} | cat 0< {clean_sh} | bash",
        # Clean feeds stay allowed (PR #2442 pins the group form).
        "{{ cat; }} < {clean_sh} | bash",
        "(cat) < {clean_sh} | bash",
        "cat {clean_sh} | cat | bash",
        # The feed reaches no program reader.
        "cat {sh} | cat",
        "cat {sh} | cat | wc -l",
        "(cat {sh}) 2>/dev/null; bash",
        "(cat {clean_sh}) 2>/dev/null | bash",
    ],
)
def test_cat_that_ignores_or_keeps_its_feed_stays_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)
