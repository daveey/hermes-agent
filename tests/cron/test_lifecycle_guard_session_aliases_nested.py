"""Session-alias regression partition: nested."""

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


# --- PR #2358 review 5894325109 nit 1: a nested login/interactive shell holds its own aliases -----
# Its startup files (~/.bash_profile, ~/.zshenv, a remote host's) define them, so the outer
# session's names say nothing about them: inside, every `NAME (` may be a keyword-led subshell.


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("names", [frozenset({"zz"}), frozenset(), None])
def test_continued_process_opener_real_bash_and_clean_twin(tmp_path, count, names):
    import shlex

    from tools.environments.local import build_subprocess_env

    script = _big(tmp_path)
    # The referenced script is harmless to execute, but exceeds the guard's scan cap.
    script.write_text("# padding\n" * 130000 + "printf RAN\n")
    payload = "shopt -s expand_aliases\nalias t='time '\n" + f"t ( bash {script} )"
    command = "bash --noprofile -l < <" + "\\\n" * count + "(printf '%s\\n' " + shlex.quote(payload) + ")"
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", command],
                            env=build_subprocess_env(), text=True, encoding="utf-8",
                            capture_output=True, timeout=10, check=True)
    assert result.stdout == "RAN"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=names)
    assert unsafe and refusal is not None and "scan cap" in refusal
    script.write_text("printf RAN\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=names) == (False, None)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("wrapper", ["printf '%s' {text}", "cat <<'EOF'\n{text}\nEOF"])
def test_continued_process_opener_inert_text(tmp_path, live_kanban_db, count, wrapper):
    import shlex

    text = "su -l bob < <" + "\\\n" * count + f"(echo t ( bash {live_kanban_db} ))"
    command = wrapper.format(text=shlex.quote(text) if wrapper.startswith("printf") else text)
    for names in (frozenset({"zz"}), frozenset()):
        assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=names) == (False, None)


@pytest.mark.parametrize("count", [7, 8, 9, 16])
@pytest.mark.parametrize("shape", [
    "{prefix}bash -l < {script}",
    "{prefix}su -l bob < {script}",
    "{prefix}sudo -i < {script}",
    "cat {script} | {prefix}su -l bob",
    "{{ {prefix}su -l bob; }} < {script}",
    "{prefix}su -l bob <<'EOF'\nt ( bash {db} )\nEOF",
])
def test_nested_shell_wrapper_boundary(tmp_path, live_kanban_db, count, shape):
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(prefix="nice " * count, script=script, db=live_kanban_db)
    for names in (frozenset({"zz"}), None):
        unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names)
        assert unsafe and refusal and "SQLite" in refusal


@pytest.mark.parametrize("shape", _NESTED_FOREIGN_SHELLS + [
    shape.replace("<(", "<" + "\\\n" * count + "(")
    for shape in _NESTED_FOREIGN_SHELLS if "<(" in shape
    for count in (1, 2)
])
def test_nested_login_shell_body_treats_names_as_unknown(tmp_path, live_kanban_db, shape):
    """Base refused these under any session alias; #2358 allowed them when the outer names
    (`zz`) missed `t`. The inner shell's own startup files decide whether `t` is an alias."""
    assert "alias" not in str(tmp_path).lower()
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(db=live_kanban_db, script=script, tmp=tmp_path)
    for names in (frozenset({"zz"}), frozenset(), None):
        unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names)
        # A source formerly inferred from a future fd definition or flattened
        # compound scope now fails closed before any file can be named.
        unresolved_source = shape in {
            "su -l bob 0<&3 3< {script}",
            "{{ su -l bob; }} 0<&3 3< {script}",
            "{{ su -l bob; }} <&3 3< {script}",
            "exec 3< {script}; {{ su -l bob; }} 0<&3",
            "su -l bob < /dev/fd/3 3< {script}",
            "su -l bob < /proc/self/fd/3 3<> {script}",
            "su -l bob 0<&3 3<<< 't ( bash {db} )'",
            "f() {{ su -l bob; }}; f 0<&3 3< {script}",
            "S={script}\nfor h in a b; do ssh $h sh -s < $S; done",
        }
        reason = "cannot resolve" if unresolved_source else "SQLite"
        assert unsafe is True and refusal is not None and reason in refusal, (shape, names)


