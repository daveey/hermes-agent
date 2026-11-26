"""Session-alias regression partition: pipes."""

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


@pytest.mark.parametrize("option", [
    "--group root", "--group=root", "-g root", "-groot",
    "--supp-group root", "--supp-group=root", "-G root", "-Groot",
    "--whitelist-environment HOME", "--whitelist-environment=HOME", "-w HOME", "-wHOME",
])
@pytest.mark.parametrize("user", ["-u bob", "-ubob", "--user bob", "--user=bob", "-mu bob"])
@pytest.mark.parametrize("first", [True, False])
def test_runuser_value_options_preserve_direct_stdin(tmp_path, live_kanban_db, option, user, first):
    """Option values must not hide direct-user mode: cat only receives inert input."""
    options = f"{option} {user}" if first else f"{user} {option}"
    text = f"runuser {options} -- cat <<< 't ( bash {live_kanban_db} )'"
    assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (
        False, None)


@pytest.mark.parametrize("option", [
    "--group", "--supp-group", "--whitelist-environment", "-g", "-G", "-w",
    "--gr", "--supp", "--whitelist", "--w",
])
def test_runuser_option_value_is_not_user_switch(tmp_path, live_kanban_db, option):
    """A flag-looking value cannot prove direct-user mode and suppress the shell walk."""
    text = f"runuser {option} -u bob <<< 't ( bash {live_kanban_db} )'"
    unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None and "SQLite" in refusal


@pytest.mark.parametrize("names", [frozenset(), frozenset({"zz"}), _STOCK_ALIASES])
def test_card_repro_python_body_in_login_shell_stays_allowed(tmp_path, live_kanban_db, names):
    """The ssh `bash -ls` repro: its only `(` openers sit in a quoted `python3 -` body, which the
    nested shell hands to Python, so they stay call arguments (mentions) under unknown names too.
    Same for that body run by a local login shell."""
    assert scan_gateway_lifecycle(_CARD_REPRO, cwd=str(tmp_path), session_aliases=names) == (
        False, None)
    body = _CARD_REPRO.split("<<'EOF'\n", 1)[1].rsplit("EOF", 1)[0]
    local = "bash -l <<'OUT'\n" + body + "OUT"
    assert scan_gateway_lifecycle(local, cwd=str(tmp_path), session_aliases=names) == (False, None)
    # Control: the same body with a shell `NAME (` call next to it in the login shell refuses.
    mixed = "bash -l <<'OUT'\n" + body + f"t ( bash {live_kanban_db} )\nOUT"
    unsafe, refusal = scan_gateway_lifecycle(mixed, cwd=str(tmp_path), session_aliases=names)
    assert unsafe is True and refusal is not None and "SQLite" in refusal


@pytest.mark.parametrize("sink,blocked", [
    ("bash -l", True), ("zsh", True), ("ssh h", True),
    ("bash", False), ("bash -s", False),
    ("bash -l -c ':'", False), ("ssh h cat", False),
])
@pytest.mark.parametrize("printer", ["printf", "echo"])
def test_printed_pipe_uses_consuming_shell_scope(tmp_path, live_kanban_db, sink, blocked, printer):
    text = f"{printer} 't ( bash {live_kanban_db} )' | {sink}"
    unsafe, refusal = scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe is blocked
    if blocked:
        assert refusal is not None and "SQLite" in refusal


@pytest.mark.parametrize("separator", [";", "&&", "||", "\n"])
def test_unrelated_login_shell_does_not_change_printed_pipe_scope(
    tmp_path, live_kanban_db, separator,
):
    text = f"printf 't ( bash {live_kanban_db} )' | bash {separator} printf ':' | bash -l"
    assert scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (False, None)


def test_login_shell_on_producing_side_does_not_receive_printed_text(tmp_path, live_kanban_db):
    text = f"bash -l | printf 't ( bash {live_kanban_db} )' | bash"
    assert scan_gateway_lifecycle(
        text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (False, None)


@pytest.mark.parametrize("shape", [
    "printf 't ( bash {db} )' | ( bash -l )",
    "( printf 't ( bash {db} )' ) | bash -l",
    "printf 't ( bash {db} )' | {{ :; bash -l; }}",
    "printf 't ( bash {db} )' |\nbash -l",
    "printf 't ( bash {db} )' |& bash -l",
    "printf 't ( bash {db} )' | cat | bash -l",
])
def test_compound_printed_pipe_keeps_nested_scope(tmp_path, live_kanban_db, shape):
    unsafe, refusal = scan_gateway_lifecycle(
        shape.format(db=live_kanban_db), cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe is True and refusal is not None and "SQLite" in refusal


def test_no_pipe_does_not_tokenize_printed_payloads(monkeypatch):
    from cron.lifecycle_guard_nested_shells import _iter_nested_printed_payloads

    def unexpected(command):
        pytest.fail("no-pipe command was tokenized")

    monkeypatch.setattr("cron.lifecycle_guard._iter_command_token_lists", unexpected)
    assert list(_iter_nested_printed_payloads("printf ':'")) == []


# PR #2928 review 5901902665 nit 1: a heredoc body is the program of the producer that owns it, so
# what that program prints into a startup shell's stdin is walked with unknown alias names too.


@pytest.mark.parametrize("shape,blocked", [
    ("python3 - <<'PY' | bash -l\n" + _PY_PRINTS + "PY", True),
    ("python3 - <<'PY' | zsh\n" + _PY_PRINTS + "PY", True),
    ("python3 - <<'PY' | ssh h\n" + _PY_PRINTS + "PY", True),
    ("python3 - <<'PY' | cat | bash -l\n" + _PY_PRINTS + "PY", True),
    ("python3 - << PY |& bash -l\n" + _PY_PRINTS + "PY", True),
    ("python3 - <<'PY' |\n" + _PY_PRINTS + "PY\nbash -l", True),
    ("python3 - <<'PY' | bash -l\nprint(\"it's\", 't ( bash {db} )')\nPY", True),
    ("cat <<'A'; python3 - <<'PY' | bash -l\nA\n" + _PY_PRINTS + "PY", True),
    # A quoted `<<x` leaves openers and bodies unmatched: the line's units all get the body.
    ("echo '<<x'; python3 - <<'PY' | bash -l\n" + _PY_PRINTS + "PY", True),
    ("{{ python3 - <<'PY'\n" + _PY_PRINTS + "PY\n}} | bash -l", True),
    # Controls: the body's output reaches no startup shell, or a plain one.
    ("python3 - <<'PY' | bash\n" + _PY_PRINTS + "PY", False),
    ("python3 - <<'PY' | bash -s\n" + _PY_PRINTS + "PY", False),
    ("python3 - <<'PY'; printf ':' | bash -l\n" + _PY_PRINTS + "PY", False),
    ("python3 - <<'PY' && printf ':' | bash -l\n" + _PY_PRINTS + "PY", False),
    ("printf ':' | bash -l | python3 - <<'PY'\n" + _PY_PRINTS + "PY", False),
    ("python3 - <<'PY' | bash -l\nprint(':')\nPY\nprintf 't ( bash {db} )' | bash", False),
])
def test_heredoc_producer_pipe_uses_consuming_shell_scope(tmp_path, live_kanban_db, shape, blocked):
    unsafe, refusal = scan_gateway_lifecycle(
        shape.format(db=live_kanban_db), cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe is blocked
    if blocked:
        assert refusal is not None and "SQLite" in refusal


# PR #2928 review 5901902665 nit 2: only a `{` (or `if`, loop) in command position opens a group,
# and a list operator or line break inside a group ends the pipe there: a later login shell reads
# the group's own stdin, not an earlier command's output.
@pytest.mark.parametrize("shape", [
    "echo {{ ; printf 't ( bash {db} )' | bash ; printf ':' | bash -l",
    "echo if ; printf 't ( bash {db} )' | bash ; printf ':' | bash -l",
    "{{ printf 't ( bash {db} )' | cat; bash -l; }}",
    "{{ printf 't ( bash {db} )' | cat && bash -l; }}",
    "{{ printf 't ( bash {db} )' | cat\nbash -l\n}}",
    "( printf 't ( bash {db} )' | cat; bash -l )",
    "{{ printf 't ( bash {db} )' | cat; bash -l; }} | cat",
    "case x in x) printf 't ( bash {db} )' | cat; bash -l;; esac",
    "echo case; printf 't ( bash {db} )' | bash; printf ':' | bash -l",
    "printf 't ( bash {db} )' | case x in bash|-l) :;; esac",
])
def test_group_pipe_does_not_reach_later_login_shell(tmp_path, live_kanban_db, shape):
    assert scan_gateway_lifecycle(
        shape.format(db=live_kanban_db), cwd=str(tmp_path),
        session_aliases=frozenset({"zz"})) == (False, None)


@pytest.mark.parametrize("shape", [
    "printf 't ( bash {db} )' | {{ cat; bash -l; }}",
    "printf 't ( bash {db} )' | ( cat; bash -l )",
    "printf 't ( bash {db} )' | {{ cat\nbash -l\n}}",
    "{{ printf 't ( bash {db} )'; }} | bash -l",
    "{{ printf 't ( bash {db} )' | cat; }} | bash -l",
    "if true; then printf 't ( bash {db} )'; fi | bash -l",
    "while :; do printf 't ( bash {db} )'; done | bash -l",
    "for x in a; do printf 't ( bash {db} )'; done | bash -l",
    "case x in x) printf 't ( bash {db} )';; esac | bash -l",
    "printf 't ( bash {db} )' | if true; then bash -l; fi",
    "printf 't ( bash {db} )' | while read -r l; do bash -l; done",
    "printf 't ( bash {db} )' | case x in x) bash -l;; esac",
    "case x in (x|y) case y in y) printf 't ( bash {db} )';; esac;; esac | bash -l",
    "case esac in (x) :;; (esac) printf 't ( bash {db} )';; esac | bash -l",
    "printf 't ( bash {db} )' | case x in\ny) :;;\nx) bash -l;;\nesac",
    "f() {{ :; }}; {{ printf 't ( bash {db} )'; }} | bash -l",
])
def test_group_output_piped_to_login_shell_keeps_nested_scope(tmp_path, live_kanban_db, shape):
    unsafe, refusal = scan_gateway_lifecycle(
        shape.format(db=live_kanban_db), cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe is True and refusal is not None and "SQLite" in refusal


def test_nested_shell_input_shapes():
    """Which nested shells hold aliases of their own, and what of theirs is shell code."""
    from cron.lifecycle_guard_nested_shells import _feeds_nested_shell, _iter_nested_shell_inputs

    for owner in ("bash -l <<'E'", "sh -i <<'E'", "zsh <<'E'", "ssh h <<'E'",
                  "ssh -p 22 -o X=1 h 'bash -s' <<'E'", "ssh h 'cd /x; bash' <<'E'",
                  "sudo -i <<'E'", "sudo -u root -s <<'E'", "su - bob <<'E'",
                  "timeout 9 bash --login <<'E'", "exec -l bash <<'E'",
                  "env su -l bob <<'E'", "nice -n 5 sudo -i <<'E'", "command doas -s <<'E'",
                  "sudo su - bob <<'E'", "exec runuser -l bob <<'E'", None):
        assert _feeds_nested_shell(owner), owner
    for owner in ("bash <<'E'", "bash -s <<'E'", "sh -e <<'E'", "python3 - <<'E'",
                  "ssh h python3 - <<'E'", "ssh h 'cat > f' <<'E'", "bash -l x.sh <<'E'",
                  "sudo -u bob python3 - <<'E'", "cat <<'E'", "env cat <<'E'",
                  "nice su -l bob -c true <<'E'", "env runuser -u bob -- cat <<'E'"):
        assert not _feeds_nested_shell(owner), owner
    found = list(_iter_nested_shell_inputs("bash -l -c 'x ( y )'; zsh s.sh; bash -c 'z'", "", cwd="/"))
    assert found == []  # *walk_command* holds the segments; *command* only its heredocs
    text = "bash -l -c 'x ( y )'; bash -c 'z'"
    assert list(_iter_nested_shell_inputs(text, text, cwd="/")) == [("x ( y )", None)]
    from cron.lifecycle_guard_nested_shells import _closes_compound

    for owner in (" } <<'E'", "} <<'E'", " fi <<E", " done <<-E", " esac <<E", " <<E", "} } <<E"):
        assert _closes_compound(owner), owner
    for owner in ("cat <<'E'", "su -l bob <<'E'", "echo } <<E", "x=} <<E"):
        assert not _closes_compound(owner), owner
    # Benign input through a compound command into a login shell stays allowed.
    benign = "{ su -l bob; } < /dev/null; { su -l bob; } <<< 'echo hi'"
    assert list(_iter_nested_shell_inputs(benign, benign, cwd="/")) == [("echo hi", None)]


# PR #2560 review 5897863903 B1: one script referenced both by a nested login shell and by the
# session shell must be walked in both scopes. A path-keyed `visited` let the masked nested walk
# suppress the session walk, whose `alias python3=...` keeps the heredoc body visible.
@pytest.mark.parametrize("shape", [
    ". s.sh",
    "bash -l s.sh; . s.sh",
    ". s.sh; bash -l s.sh",
    "bash -l -c 'bash s.sh'; . s.sh",
    "bash -l -c '. s.sh'; source s.sh",
])
def test_script_shared_by_nested_and_session_scope_is_walked_in_both(tmp_path, shape):
    (tmp_path / "s.sh").write_text(_FN_BODY)
    unsafe, _refusal = scan_gateway_lifecycle(
        shape, cwd=str(tmp_path), session_aliases=frozenset({"python3"}))
    assert unsafe is True, shape


