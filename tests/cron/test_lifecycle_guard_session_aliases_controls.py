"""Session-alias regression partition: controls."""

import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments.base_session_env import _alias_names_printf, _split_alias_marker
from tools.terminal_tool_guards import _session_aliases_for_guard, gateway_lifecycle_block

from tests.cron.lifecycle_guard_session_aliases_shared import (
    _FN_BODY,
    _big,
    _CARD_REPRO,
    _STOCK_ALIASES,
    live_kanban_db as live_kanban_db,  # noqa: PLC0414 -- pytest fixture reexport
    _NESTED_FOREIGN_SHELLS,
    _PY_PRINTS,
    _PY_TAKEOVER,
    _MULTI_HOP_DATA_CONSUMERS,
    _MULTI_HOP_SHELLS,
)


@pytest.mark.parametrize("shape", [
    "bash <<'EOF'\nt ( bash {db} )\nEOF",  # a non-login, non-interactive child inherits no aliases
    "bash -s <<'EOF'\nt ( bash {db} )\nEOF",
    "t2 ( bash {db} )",
    "bash <<< 't ( bash {db} )'",
    "bash < <(cat {script})",
    "bash -s < <(cat {script})",
    "cat < <(cat {script})",
    "su -l bob -c true < <(cat {script})",
    "bash < {script}",
    "exec -a x bash < {script}",
    "exec -a bash cat < {script}",
    "exec -a su cat < {script}",
    "bash -l 3<<< 't ( bash {db} )'",
    "ENV=x sh -c 't ( bash {db} )'",
    "BASH_ENV=x sh -c 't ( bash {db} )'",
    "OTHER=x bash -c 't ( bash {db} )'",
    "doas python3 - <<'EOF'\nprint('t ( bash {db} )')\nEOF",
    "runuser -u bob -- python3 - <<'EOF'\nprint('t ( bash {db} )')\nEOF",
    "runuser -ubob -- python3 - <<'EOF'\nprint('t ( bash {db} )')\nEOF",
    "runuser --user bob -- python3 - <<'EOF'\nprint('t ( bash {db} )')\nEOF",
    "runuser --user=bob -- python3 - <<'EOF'\nprint('t ( bash {db} )')\nEOF",
    "runuser -u bob -- printf '%s\\n' --command='t ( bash {db} )'",
    "runuser -u bob -- printf '%s\\n' -c 't ( bash {db} )'",
    "runuser -u bob printf '%s\\n' --command='t ( bash {db} )'",
    "runuser --user=bob printf '%s\\n' -c 't ( bash {db} )'",
    "runuser --us bob -- cat <<< 't ( bash {db} )'",
    "runuser --u=bob -- printf '%s\\\\n' -c 't ( bash {db} )'",
    "runuser --white HOME -u bob -- cat <<< 't ( bash {db} )'",
    "runuser --supp=root --us=bob -- cat <<< 't ( bash {db} )'",
    # Direct-user runuser and non-shell commands read a stdin file as data.
    "runuser -fu bob -- cat <<< 't ( bash {db} )'",
    "runuser -f -u bob -- cat <<< 't ( bash {db} )'",
    "runuser -u bob -- cat < {script}",
    "runuser -fu bob -- cat < {script}",
    "cat < {script}",
    "sudo cat < {script}",
    "doas cat < {script}",
    "ssh host cat < {script}",
    # A compound command's redirection with no login shell inside keeps the session's names.
    "{{ cat; }} < {script}",
    "( bash ) < {script}",
    "{{ bash -s; }} <<< 't ( bash {db} )'",
    "{{ cat; }} <<'EOF'\nt ( bash {db} )\nEOF",
    "while read l; do echo \"$l\"; done < {script}",
    "{{ su -l bob; }} 3< {script}",  # not stdin
    # A login shell reading stdin elsewhere does not make other commands' input its own.
    "su -l bob < /dev/null; cat < {script}",
    "su -l bob < /dev/null; cat <<'EOF'\nt ( bash {db} )\nEOF",
    "su -l bob -c true; {{ cat; }} < {script}",
    # /dev/null feeds a login shell nothing.
    "su -l bob < /dev/null",
    "ssh host < /dev/null",
    "{{ su -l bob; }} < /dev/null",
    # A piped file or a multi-line group's redirection with no login shell reading it.
    "cat {script} | cat",
    "{{\ncat\n}} < {script}",
    "(\ncat\n) < {script}",
    "(\nbash\n) < {script}",
    "{{\nbash\n}} < {script}",
    "while true; do\nbash\ndone < {script}",
    "cat {script}; su -l bob",
    "su -l bob | cat {script}",
    "cat {script} | su -l bob -c true",
    # Printed text in a `<(…)` read as stdin by no login shell keeps session scope.
    "bash < <(echo 't ( bash {db} )')",
    "bash -s < <(printf '%s\\n' 't ( bash {db} )')",
    "cat < <(echo 't ( bash {db} )')",
    *[shell + " < <" + "\\\n" * count + "(echo 't ( bash {db} )')"
      for shell in ("bash", "cat") for count in (1, 2)],
    "su -l bob -c true < <(echo 't ( bash {db} )')",
    "su -l bob; cat < <(echo 't ( bash {db} )')",
    "{{ su -l bob; cat < <(echo 't ( bash {db} )'); }}",
    "su -l bob < <(echo ':'); echo 't ( bash {db} )'",
    # A wrapper around a non-shell command, or a login shell given its own `-c`.
    "env cat < {script}",
    "nice cat < {script}",
    "timeout 5 cat < {script}",
    "cat {script} | env cat",
    "env su -l bob -c true < {script}",
    "nice su -l bob -c true <<< 't ( bash {db} )'",
    "sudo cat < {script}",
    "env runuser -u bob -- cat < {script}",
    # A duplicated descriptor feeding no login shell, or a closed/own stdin (t_01a1daf4b486a755).
    "cat 0<&3 3< {script}",
    "exec 3< {script}; cat <&3",
    "su -l bob 0<&-",
    "su -l bob 0<&0",
    # An unresolvable source that feeds no login shell (t_01a1daf4b486a755).
    "cat < $S",
    "{{ cat; }} < <(cat {script})",
    "S={script}; cat < $S; su -l bob < /dev/null",
    "while read l; do echo \"$l\"; done < <(cat {script})",
    # A function call's redirection reaches only shells the command runs (t_01a1daf4b486a755).
    "f() {{ cat; }}; f < {script}",
    "f() {{ su -l bob; }}; cat < {script}",
    "f() {{ su -l bob; }}; cat <<'EOF'\nt ( bash {db} )\nEOF",
    "f() {{ su -l bob; }}; f < /dev/null",
])
def test_plain_child_shell_keeps_session_names(tmp_path, live_kanban_db, shape):
    """Controls: no login/interactive shell, so the session's names still decide (#2358)."""
    assert "alias" not in str(tmp_path).lower()
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(db=live_kanban_db, script=script)
    assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (
        False, None)


@pytest.mark.parametrize("shape", [
    "( bash ) < {script}",  # one line: refused before this change too
    # The group spans lines (t_01a1dae7a625be0c): the session walk read one line at a time, so
    # the closer's redirection or the pipe never reached the shell lines earlier.
    "(\nbash\n) < {script}",
    "{{\nbash\n}} < {script}",
    "while true; do\nbash\ndone < {script}",
    "if true; then\nsh\nfi < {script}",
    "{{\ntrue\n{{\nbash\n}}\n}} < {script}",
    "cat {script} | {{\nbash\n}}",
    "cat {script} | while read l; do\nbash\ndone",
    "cat {script} | (\nbash\n)",
])
def test_multiline_group_feeds_its_plain_shell_in_session_walk(tmp_path, live_kanban_db, shape):
    """With unknown names the walked file's `t (` may be a subshell: refused as on one line."""
    assert "alias" not in str(tmp_path).lower()
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(script=script)
    unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=None)
    assert unsafe is True and refusal is not None and "SQLite" in refusal, shape
    assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (
        False, None)


@pytest.mark.parametrize("shape", [
    "{{\ncat\n}} < {script}",
    "(\ncat\n) < {script}",
    "{{\nbash\n}}\ncat < {script}",  # the group closed before the redirected command
    "{{\nbash\n}}; cat {script} | cat",
    "cat {script} | {{\ncat\n}}\nbash",  # the pipe feeds only the group
])
def test_multiline_group_without_a_fed_shell_stays_allowed(tmp_path, live_kanban_db, shape):
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(script=script)
    assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=None) == (False, None)


@pytest.mark.parametrize("shape", [
    # cat process substitutions resolve to their file and are covered above.
    "su -l bob < $S",
    "su -l bob < \"$S\"",
    "su -l bob < ${{S}}",
    "su -l bob < $(echo {script})",
    "S=$(echo {script}); su -l bob < $S",  # an assigned value the walk cannot name
    "S={script} su -l bob < $S",  # a prefix assignment does not reach the redirection
    "su -l bob < \"$1\"",
    "for f in {script}; do su -l bob < $f; done",
    "su -l bob <$(echo {script})",
    "su -l bob < `echo {script}`",
    "su -l bob < {tmp}/*.sh",
    "{{ su -l bob; }} < $S",
    "su -l bob 0<&3",  # a descriptor with no source in the command
    "su -l bob 0<&$fd",
    "su -l bob 0<&3 3< $S",
    "su -l bob 0<&3 3<<'EOF'\nt ( bash {db} )\nEOF",
])
def test_nested_login_shell_unresolvable_stdin_fails_closed(tmp_path, live_kanban_db, shape, monkeypatch):
    """t_01a1daf4b486a755: a stdin whose content the walk cannot name (a variable, a command or
    process substitution, a glob, an unknown descriptor) fed to a login shell is refused, not
    skipped as if it held nothing."""
    monkeypatch.delenv("S", raising=False)
    assert "alias" not in str(tmp_path).lower()
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(db=live_kanban_db, script=script, tmp=tmp_path)
    for names in (frozenset({"zz"}), frozenset(), None):
        unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names)
        assert unsafe is True and refusal is not None and "cannot resolve" in refusal, (shape, names)


def test_unresolvable_login_shell_stdin_in_a_mentioned_file_is_not_refused(tmp_path):
    """Only executed text fails closed: a file a Python body merely opens is a mention."""
    script = tmp_path / "s.sh"
    script.write_text("su -l bob < $S\n")
    mention = f"python3 - <<'PY'\nprint(open('{script}').read())\nPY"
    assert scan_gateway_lifecycle(mention, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (
        False, None)
    unsafe, refusal = scan_gateway_lifecycle(f"bash {script}", cwd=str(tmp_path),
                                             session_aliases=frozenset({"zz"}))
    assert unsafe is True and refusal is not None and "cannot resolve `$S`" in refusal


def test_nested_login_shell_stdin_variable_from_the_environment(tmp_path, live_kanban_db,
                                                                 monkeypatch):
    """A variable the command does not assign expands as the guard's own environment holds it
    (`$TMPDIR`, `$HERMES_KANBAN_WORKSPACE`: the local terminal inherits them)."""
    (tmp_path / "inner.sh").write_text(f"t ( bash {live_kanban_db} )\n")
    monkeypatch.setenv("T01A1DAF4_DIR", str(tmp_path))
    for text in ("ssh host 'bash -s' < \"$T01A1DAF4_DIR/inner.sh\"",
                 "su -l bob < ${T01A1DAF4_DIR}/inner.sh"):
        unsafe, refusal = scan_gateway_lifecycle(text, cwd="/", session_aliases=frozenset({"zz"}))
        assert unsafe is True and refusal is not None and "SQLite" in refusal, text
    monkeypatch.delenv("T01A1DAF4_DIR")
    unsafe, refusal = scan_gateway_lifecycle("su -l bob < $T01A1DAF4_DIR/inner.sh", cwd="/",
                                             session_aliases=frozenset({"zz"}))
    assert unsafe is True and refusal is not None and "cannot resolve" in refusal


