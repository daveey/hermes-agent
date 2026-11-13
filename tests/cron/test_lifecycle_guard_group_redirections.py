"""Stdin feeds into nested and redirected groups (PR #2316 review 5894173327).

Each case below ran a lifecycle ``.py``/``.sh`` on stdin the guard never opened:

1. A nested brace group (``cat f.sh | { { true; }; bash; }``): the inner ``}`` closed the outer
   group's feed too.
2. A pipe into a compound command (``cat f.sh | if true; then bash; fi``, ``| while …; done``).
3. A stdin redirection on a group (``{ true; bash; } < f.sh``, ``(bash) < f.sh``): it follows the
   closer, after the group's commands were read, and fed none of them.
4. An environment assignment glued to ``<`` (``X=1<f.py python3``): the assignment pattern took
   the whole word, so the redirection was never split off.
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
    clean = tmp_path / "clean.py"
    clean.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    clean_sh = tmp_path / "clean.sh"
    clean_sh.write_text("echo ok\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean": clean, "clean_sh": clean_sh}.items()}


@pytest.mark.parametrize(
    "template",
    [
        # 1. Nested brace groups keep the outer feed.
        "cat {sh} | {{ {{ true; }}; bash; }}",
        "cat {sh} | {{ {{ {{ true; }}; }}; bash; }}",
        "cat {py} | {{ {{ true; }}; {{ :; }}; python3; }}",
        "cat {sh} | ( {{ true; }}; bash )",
        # 2. A pipe into a compound command.
        "cat {sh} | if true; then bash; fi",
        "cat {sh} | while read l; do bash; done",
        "cat {py} | until false; do python3; done",
        # 3. A stdin redirection on a group feeds every command in it.
        "{{ true; bash; }} < {sh}",
        "{{ bash; }} < {sh}",
        "{{ bash; }} <{sh}",
        "{{ bash; }} 0< {sh}",
        "{{ true; python3; }} < {py}",
        "(bash) < {sh}",
        "(true; bash) <{sh}",
        "( python3 ) < {py}",
        "{{ {{ true; }}; bash; }} < {sh}",
        "{{ {{ bash; }}; }} < {sh}",
        "if true; then bash; fi < {sh}",
        "while read l; do bash; done < {sh}",
        "until false; do python3; done < {py}",
        "for x in 1; do bash; done < {sh}",
        "case x in *) bash;; esac < {sh}",
        "{{ true; bash; }} > /dev/null < {sh}",
        "true; {{ bash; }} < {sh}",
        "cat {clean_sh} | {{ bash; }} < {sh}",
        # 4. An environment assignment glued to `<`.
        "X=1<{py} python3",
        "X=1 Y=2<{py} python3",
        "X=1<{sh} bash",
        "env X=1<{py} python3",
    ],
)
def test_group_and_glued_stdin_feeds_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # A group redirection feeds that group only.
        "{{ true; }} < {sh}; bash {clean_sh}",
        "(true) < {sh}; bash {clean_sh}",
        "{{ bash {clean_sh}; }} < {sh}",
        "{{ true; }} < {sh} | bash {clean_sh}",
        "{{ {{ true; }} < {sh}; bash {clean_sh}; }}",
        "{{ true; }} > {sh}",
        "(true) 2< {sh}",
        "while read l; do echo $l; done < {sh}",
        "{{ cat; }} < {clean_sh} | bash",
        # Data: the redirection names the file, nothing runs it.
        "echo '{{ bash; }} < {sh}'",
        "grep -c x < {sh}",
        "X=1 grep x <{py}",
        "X='1<{py}' python3 {clean}",
    ],
)
def test_group_redirections_and_data_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)
