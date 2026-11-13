"""Pins for conditions of PR #2393 that no other test reaches (post-merge check t_01a1d5e5c0b52228).

Each case fails when its condition is dropped and passes on the real guard; real bash runs every
"scanned" shape the way the comment says.

- A backslash inside single quotes is literal, so ``'a\\'`` closes the quote and the later glued
  ``-<f.py`` is still split (``split_glued_redirections``).
- A control character (``;``) ends a word, so ``0<f.py`` after ``true;`` is a redirect.
- A line shlex rejects (``echo 'a\\' $'b\\'c'``, valid bash) falls back to a per-physical-line
  split that still splits glued ``<``.
- An unclosed ``<(`` at line end is still a program file (``python3 <(cat f.py;`` NEWLINE ``)``).
- A pipe inside ``<( ... )`` does not replace the outer stdin feed: ``cat c.py | python3 -
  <(cat f.py | cat)`` reads its program from c.py, not f.py.
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
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean": clean}.items()}


@pytest.mark.parametrize(
    "template",
    [
        # Backslash inside single quotes is literal: the quote closes, `-<f.py` is a redirect.
        "echo 'a\\'; python3 -<{py}",
        "echo 'a\\' && bash -s<{sh}",
        # A control character ends the word, so `0<f.py` is a redirect of python3's stdin.
        "true;0<{py} python3",
        # The guard's line splitter and shlex both see an open quote on line 1 (bash closes
        # `'a\'` and reads `$'b\'c'` as ANSI-C quoting), so the per-physical-line fallback runs
        # and must still split the glued `<` on line 2.
        "echo 'a\\' $'b\\'c'\npython3 -<{py}",
        # An unclosed `<(` at line end still feeds the program file.
        "python3 <(cat {py};\n)",
    ],
)
def test_pinned_lifecycle_feeds_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


def test_pipe_inside_process_substitution_keeps_outer_stdin_feed(files):
    cmd = "cat {clean} | python3 - <(cat {py} | cat)".format(**files)
    assert scan_gateway_lifecycle(cmd) == (False, None)
