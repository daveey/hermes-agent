"""A script fed on a descriptor moved, chained or opened by an earlier ``exec`` is read
(t_01a1d6ecbe38ab83).

``bash 3<f.sh <&3`` was blocked, but the move form ``bash 3<f.sh <&3-`` (target ``3-``) and a
descriptor opened by an earlier ``exec`` (``exec 3<f.sh; bash <&3``) let ``bash`` read ``f.sh``
unscanned. So did ``0>&3`` (``dup2(3, 0)`` like ``0<&3``), a chain (``4<&3 <&4``) and a
redirection written before the command (``3<f.sh bash <&3``), which the hoist moved after the
``<&3`` that reads it.
"""

from __future__ import annotations

import shlex

import pytest

from cron import lifecycle_guard
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
        # The card's move form.
        "bash 3<{sh} <&3-",
        "bash 3<{sh} 0<&3-",
        "bash 3< {sh} <& 3-",
        # Other ways one command puts the opened file on stdin.
        "bash 3<{sh} 0>&3",
        "bash 3<{sh} 4<&3 <&4",
        "bash 3<{sh} 4<&3- <&4",
        "3<{sh} bash <&3",
        "3<{sh} cat <&3 | bash",
        "exec 3<{sh} bash <&3",
        "cat 3<{sh} <&3- | bash",
        "python3 3<{py} <&3-",
        # The card's cross-command form: `exec` with only redirections keeps them open.
        "exec 3<{sh}; bash <&3",
        "exec 3<{sh}\nbash <&3",
        "exec 3< {sh}; bash 0<& 3",
        "exec 3<{sh} && bash <&3",
        "exec 3<{sh}; bash <&3-",
        "exec 3<{sh}; bash 0>&3",
        "exec 3<{sh} 4<&3; bash <&4",
        "exec 3<{sh}; exec 4<&3-; bash <&4",
        "exec 3<{sh}; exec 4<&3; exec 3<&-; bash <&4",
        "exec 3<{sh}; cat <&3 | bash",
        "exec 3<{sh}; {{ bash; }} <&3",
        "exec 3<{py}; python3 <&3",
        "exec <{sh}; bash <&0",
        "{{ exec 3<{sh}; }}; bash <&3",
        "if true; then exec 3<{sh}; fi; bash <&3",
        "exec 3<{clean_sh}; exec 3<{sh}; bash <&3",
        "exec 3<{sh}; bash 4<&3- <&4",
        "exec 3<{sh}; bash -s '3<&-' <&3",
        "exec 3<{sh}; bash -s '3>/dev/null' <&3",
        "exec 3<{sh}; bash -s <&3 '0<&-'",
        "exec 3<{sh}; bash -s <&3 '0>/dev/null'",
        "exec 3<{sh}; bash 1>&3 <&1",
        # Leading-redirection and wrapped `exec` still keep their redirections.
        "3<{sh} exec; bash <&3",
        "<{sh} exec; bash <&0",
        "command exec 3<{sh}; bash <&3",
        "command -p exec 3<{sh}; bash <&3",
        "builtin exec 3<{sh}; bash <&3",
        "builtin -- exec 3<{sh}; bash <&3",
        "3<{sh} command exec; bash <&3",
        # A conditional close or rebind keeps the earlier dangerous open as an alternative.
        "exec 3<{sh}; if false; then exec 3<&-; fi; bash <&3",
        "exec 3<{sh}; if false; then exec 3<{clean_sh}; fi; bash <&3",
        "exec 3<{sh}; false && exec 3<&-; bash <&3",
        "exec 3<{sh}; true || exec 3<{clean_sh}; bash <&3",
        "exec 3<{sh}; while false; do exec 3<&-; done; bash <&3",
        "exec 3<{sh}; {{ exec 3<&-; }}; bash <&3",
        "exec 3<{sh}; case x in y) exec 3<&- ;; esac; bash <&3",
        "exec 3<{sh}; if x; then exec 4<&3-; fi; bash <&3",
        "exec 3<{sh}; if x; then exec 3<&-; fi; exec 4<&3; bash <&4",
        "exec 3<{sh}; if x; then exec 3<{clean_sh}; fi; if y; then exec 3<&-; fi; bash <&3",
        "exec 3<{clean_sh}; if x; then exec 3<{sh}; fi; if y; then exec 3<&-; fi; bash <&3",
        "exec 3<{sh}; time if false; then exec 3<&-; fi; bash <&3",
        "exec 3<{sh}; function f {{ exec 3<&-; }}; bash <&3",
        # Distinct alternatives must not evict a reachable dangerous middle target.
        "exec 3<{clean_sh}; "
        + "".join("if x; then exec 3<{clean_sh}." + str(i) + "; fi; " for i in range(8))
        + "if x; then exec 3<{sh}; fi; "
        + "".join("if x; then exec 3<{clean_sh}." + str(i) + "; fi; " for i in range(8, 40))
        + "bash <&3",
    ],
)
def test_descriptor_fed_script_is_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # A duplication before the open reads nothing: the descriptor is not open yet.
        "bash <&3 3<{sh}",
        "bash <&3- 3<{sh}",
        "bash <&3; exec 3<{sh}",
        # A clean script stays clean on every path.
        "bash 3<{clean_sh} <&3-",
        "exec 3<{clean_sh}; bash <&3",
        "exec 3<{clean_sh}; bash <&3-",
        # The command's own open wins over the carried one.
        "exec 3<{sh}; bash 3<{clean_sh} <&3",
        # Closed, re-pointed or moved away before the read.
        "exec 3<{sh}; exec 3<&-; bash <&3",
        "exec 3<{sh}; exec 3>/dev/null; bash <&3",
        "exec 3<{sh}; exec 3<{clean_sh}; bash <&3",
        "exec 3<{sh}; exec 4<&3-; bash <&3",
        # Another descriptor, or none, reads no carried file.
        "exec 3<{sh}; bash <&4",
        "exec 3<{sh}; bash",
        # `exec` that runs a command replaces the shell: its redirections are that command's.
        "exec 3<{sh} true; bash <&3",
        # A writing duplication on stdout is not stdin.
        "bash 3<{sh} >&3",
        "exec 3<{sh}; bash >&3",
        "exec 3<{sh}; bash <&3 <{clean_sh}",
        "exec 3<{sh}; bash 4<&3- <&3",
        "exec 3<{sh}; bash 3<&3- <&3",
        "exec 3<{sh}; bash 3>&- <&3",
        "exec 3<{sh}; bash '<&3'",
        "exec '3<{sh}'; bash <&3",
        "command exec 3<{clean_sh}; bash <&3",
        "command -v exec 3<{sh}; bash <&3",
        "command exec 3<{sh} true; bash <&3",
        "builtin exec 3<{sh} true; bash <&3",
        "echo exec 3<{sh}; bash <&3",
        "3<{clean_sh} exec; bash <&3",
        # An unconditional close/rebind after a conditional one wins.
        "exec 3<{sh}; if x; then exec 3<{clean_sh}; fi; exec 3<&-; bash <&3",
        "exec 3<{sh}; if x; then :; fi; exec 3<{clean_sh}; bash <&3",
        "exec 3<{sh}; if x; then :; fi; command exec 3<&-; bash <&3",
        "if x; then exec 3<{clean_sh}; fi; bash <&3",
        "exec 3<{clean_sh}; if x; then exec 3<{clean_sh}; fi; bash <&3",
    ],
)
def test_unread_or_clean_descriptor_stays_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


def test_in_order_hoist_keeps_shell_order():
    hoist = lifecycle_guard._hoist_command_redirections
    assert hoist(["3<f", "bash", "<&3"]) == ["bash", "3<f", "<&3"]
    assert hoist(["sudo", "<", "f", "bash", "-x"]) == [
        "sudo", "bash", "<", "f", "-x"]
    assert hoist(["<"]) == ["<"]


@pytest.mark.parametrize(
    ("arguments", "stdin"),
    [
        (["3<f", "<&3-"], ["f"]),
        (["3<f", "0>&3"], ["f"]),
        (["3<f", "4<&3-", "<&3"], []),
        (["3<f", ">&3"], []),
        (["<&3", "3<f"], []),
        (["<", "f", "<&0"], ["f"]),
    ],
)
def test_split_redirections_follows_descriptors(arguments, stdin):
    assert lifecycle_guard._split_redirections(arguments) == ([], stdin)
