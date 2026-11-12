"""The referenced-script walk sees the same inert-heredoc-masked view as the direct scan (#110422).

``_direct_lifecycle_scan`` masks provably-inert heredoc bodies (quoted delimiter, allowlisted
data consumer such as ``python3 - <<'PY'``), but ``_contains_unsafe_gateway_action`` walked
referenced scripts and ``sh -c`` payloads on the raw command, so a >1 MiB data path mentioned
inside the Python body failed closed as an oversized "script".
"""

from pathlib import Path

import pytest

from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script as guard


def _big_file(tmp_path):
    path = tmp_path / "big_blob.bin"
    path.write_bytes(b"\0" * (2 * 1024 * 1024))
    return path


def test_inert_heredoc_body_path_not_walked_as_script(tmp_path):
    big = _big_file(tmp_path)
    command = f"python3 - <<'PY'\nfrom pathlib import Path\nprint(Path('{big}').stat().st_size)\nPY"
    assert guard(command, cwd=str(tmp_path)) is False


def test_prefixed_inert_heredoc_body_path_not_walked_as_script(tmp_path):
    big = _big_file(tmp_path)
    command = (
        f"cd {tmp_path} && python3 - <<'PY'\n"
        f"from pathlib import Path\nprint(Path('{big}').stat().st_size)\nPY"
    )
    assert guard(command, cwd=str(tmp_path)) is False


def test_allowlisted_name_function_heredoc_stays_visible(tmp_path):
    command = "python3() { bash; }; python3 <<'PY'\nhermes gateway restart\nPY"
    assert guard(command, cwd=str(tmp_path)) is True


def test_unquoted_heredoc_body_path_still_walked(tmp_path):
    """An expansion-capable body is not provably inert: the walk still sees it and fails closed."""
    big = _big_file(tmp_path)
    assert guard(f"cat > /tmp/x <<EOF\n{big}\nEOF", cwd=str(tmp_path)) is True


def test_inert_heredoc_body_script_path_still_read(tmp_path):
    """Masking hides the body from the *executed* view only: a lifecycle script named inside a
    Python body is still handed to ``os.system`` at runtime, so its contents must still be read."""
    script = tmp_path / "restart.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    command = f"python3 - <<'PY'\nimport os\nos.system('{script}')\nPY"
    assert guard(command, cwd=str(tmp_path)) is True


def test_mentioned_data_file_that_cannot_be_scanned_is_not_a_verdict(tmp_path, monkeypatch):
    """A file only MENTIONED in an inert body may exhaust the text budget (one >64 KiB line), pull
    in 64+ remote-read misses (a markdown table of paths) or be a live SQLite database: each is
    "nothing to scan", never a block (#113944). The same file *executed* still fails closed."""
    from cron import lifecycle_guard
    from hermes_cli.sqlite_safe_read import connect_tracked

    minified = tmp_path / "minified.json"
    minified.write_text("[" + "1," * 40000 + "1]", encoding="utf-8")
    notes = tmp_path / "notes.md"
    notes.write_text("\n".join(f"| /opt/frag{i}/tool-{i}.md | note |" for i in range(70)), encoding="utf-8")
    db = tmp_path / "state.db"
    conn = connect_tracked(db)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 8)
    remote_misses: list[str] = []

    def remote(path: str):
        remote_misses.append(path)

    try:
        for data in (minified, notes, db):
            command = f"cd {tmp_path} && python3 - <<'PY'\nt = open('{data}', encoding='utf-8').read()\nPY"
            assert guard(command, cwd=str(tmp_path), read_remote_script=remote) is False, data.name
        assert remote_misses  # the notes table was walked and its misses were bounded, not fatal
        unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(f"bash {minified}")
        assert unsafe is True and "over the 65536-character per-line scan cap" in refusal
        unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(f"bash {db}")
        assert unsafe is True and "SQLite" in refusal
    finally:
        conn.close()


def test_mentioned_script_with_lifecycle_command_still_blocks(tmp_path):
    """The lenient path only covers "could not scan": a mentioned script whose text IS a lifecycle
    command is still a positive verdict, and the refusal reason stays empty (it is not a scan failure)."""
    from cron import lifecycle_guard

    script = tmp_path / "restart.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    command = f"cd {tmp_path} && python3 - <<'PY'\nimport os\nos.system('{script}')\nPY"
    assert lifecycle_guard.scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("shape", ["oversized", "sqlite"])
def test_unscannable_own_cron_script_raises_named_refusal(tmp_path, shape):
    """A cron job whose OWN script is oversized or a live SQLite database fails closed with the
    named "could not scan" refusal, never the misattributed "contains a gateway lifecycle command"."""
    from cron.lifecycle_guard import GatewayLifecycleBlocked, check_gateway_lifecycle
    from hermes_cli.sqlite_safe_read import connect_tracked

    if shape == "oversized":
        script, conn = _big_file(tmp_path), None
    else:
        script = tmp_path / "state.db"
        conn = connect_tracked(script)
    try:
        with pytest.raises(GatewayLifecycleBlocked) as excinfo:
            check_gateway_lifecycle("run it", str(script))
    finally:
        if conn is not None:
            conn.close()
    message = str(excinfo.value)
    assert "could not scan" in message and str(script) in message
    assert "contains a gateway lifecycle command" not in message


# --- wrapped, function-body and nested data consumers (t_01a1d0ea6db6c158) --------------------
# Each shape below opened a >1 MiB SQLite file read-only and was refused as an "executed script":
# the consumer was only recognized as a bare top-level `python3 - <<'PY'`.

_FIXTURES = Path(__file__).parent / "fixtures" / "lifecycle_guard_heredoc"


def _big_sqlite(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SQLite format 3\0" + b"\0" * (2 * 1024 * 1024 - 16))
    return path


def _bodies(db):
    # one-line (the probe table) and a wrapped call whose path line starts a shell segment
    return {
        "one-line": f"import sqlite3\nsqlite3.connect('{db}')\n",
        "multi-line": f"import sqlite3\nc = sqlite3.connect(\n    '{db}')\n",
    }


_CONSUMER_SHAPES = {
    "wrapper": "timeout 20 python3 - <<'PY'\n{body}PY",
    "wrapper-args": "nice -n 5 env A=1 timeout 20 python3 - \"$T\" x <<'PY'\n{body}PY",
    "nested-in-ssh": "ssh -o BatchMode=yes h \"bash -s\" <<'R'\npython3 - <<'PY'\n{body}PY\nR",
    "nested-$SSH": "SSH='ssh -o BatchMode=yes'\n$SSH h \"bash -s\" <<'R' 2>&1 | grep -v x\n"
                   "python3 - <<'PY'\n{body}PY\nR",
    "function-args": "q() {{ python3 - \"$1\" <<'PY'\n{body}PY\n}}\nq x",
    "function-no-args": "q() {{ python3 - <<'PY'\n{body}PY\n}}\nq x",
    "function-keyword": "function q {{ timeout 20 python3 - \"$1\" <<'PY'\n{body}PY\n}}\nq x",
    "nested-function-wrapper": "ssh h \"bash -s\" <<'R'\nq() {{ timeout 20 python3 - \"$1\" <<'PY'\n"
                               "{body}PY\n}}\nq x\nR",
}


@pytest.mark.parametrize("body_kind", ["one-line", "multi-line"])
@pytest.mark.parametrize("shape", sorted(_CONSUMER_SHAPES))
def test_data_consumer_heredoc_mention_is_not_refused(tmp_path, shape, body_kind):
    from cron.lifecycle_guard import scan_gateway_lifecycle
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    db = _big_sqlite(tmp_path / "big.db")
    command = _CONSUMER_SHAPES[shape].format(body=_bodies(db)[body_kind])
    masked = strip_inert_heredoc_bodies(command, nested=True)  # the executed-path walk's view
    assert str(db) not in masked, masked  # the consumer body is masked, wherever it sits
    assert masked.count("\n") == command.count("\n")
    # Text scanners still see a NESTED body (its outer body could rewrite it); others are masked.
    assert (str(db) in strip_inert_heredoc_bodies(command)) == shape.startswith("nested")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


# Shapes where a shell really runs the "consumer" body (or rewrites it first): main refuses each,
# and so must the peeled/nested masking (review of PR #1671 @004c14deb336, plus the function
# output and stdin-hijack variants of the same class).
_SHELL_RUNS_BODY = {
    "exec-a": "exec -a python3 bash <<'R'\nhermes gateway restart\nR",
    "env-S-attached-single": "env -S'bash -s' python3 <<'R'\nhermes gateway restart\nR",
    "env-S-attached-double": "env -S\"bash -s\" python3 <<'R'\nhermes gateway restart\nR",
    "env-split-string=": "env --split-string='bash -s' python3 <<'R'\nhermes gateway restart\nR",
    "sudo-D": "sudo -D python3 bash <<'R'\nhermes gateway restart\nR",
    "sudo-R": "sudo -R python3 bash <<'R'\nhermes gateway restart\nR",
    "sudo-chdir": "sudo --chdir=/ python3 - <<'R'\nhermes gateway restart\nR",
    "outer-tab-strip": "bash <<-'R'\n\tpython3 - <<'PY'\n\tprint(1)\n\tPY\n"
                       "\thermes gateway restart\nPY\nR",
    "outer-transform": "sed 's/python3 - /bash -s /' <<'R' | bash\npython3 - <<'PY'\n"
                       "hermes gateway restart\nPY\nR",
    "outer-unknown-var": "$CMD <<'R'\npython3 - <<'PY'\nhermes gateway restart\nPY\nR",
    "outer-stdin-hijack": "bash -s <<'R'\nsed 's/^python3 - /bash -s /' | bash\npython3 - <<'PY'\n"
                          "hermes gateway restart\nPY\nR",
    "outer-ssh-remote-pipe": "ssh h 'sed s/python3/bash/ | bash' <<'R'\npython3 - <<'PY'\n"
                             "hermes gateway restart\nPY\nR",
    "fn-cat-piped": "f() { cat <<'PY'\nhermes gateway restart\nPY\n}\nf | bash",
    "fn-python-piped": "f() { python3 - <<'PY'\nprint('hermes gateway restart')\nPY\n}\nf | bash",
    "fn-python-eval": "f() { python3 - <<'PY'\nprint('hermes gateway restart')\nPY\n}\neval \"$(f)\"",
    "ssh-shadow": "ssh() { sed s/python3/bash/ | bash; }\nssh h 'bash -s' <<'R'\npython3 - <<'PY'\n"
                  "hermes gateway restart\nPY\nR",
}


@pytest.mark.parametrize("shape", sorted(_SHELL_RUNS_BODY))
def test_bodies_a_shell_runs_stay_refused(tmp_path, shape):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    assert scan_gateway_lifecycle(_SHELL_RUNS_BODY[shape], cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("command", [
    # wrappers outside the stdin-passing allowlist, or with options it does not know
    "exec -a python3 bash <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "env -S'bash -s' python3 - <<'PY'\nx\nPY",
    "sudo -D / python3 - <<'PY'\nx\nPY",
    # nested only under a single, tab-preserving body that a stdin shell reads verbatim
    "bash <<-'R'\npython3 - <<'PY'\nx\nPY\nR",
    "sed s/a/b/ <<'R' | bash\npython3 - <<'PY'\nx\nPY\nR",
    "bash -c 'sed s/a/b/ | bash' <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "bash script.sh <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "ssh h 'cat | bash' <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "ssh h 'bash -s | bash' <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "SSH=\"$(pick)\"\n$SSH h 'bash -s' <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    # a shadowed outer shell/ssh, and `cat` (its output is its body) inside a function
    "ssh() { cat; }\nssh h 'bash -s' <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "f() { cat <<'PY'\nx\nPY\n}\nf",
])
def test_untrusted_owner_masks_nothing(command):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("command", [
    "timeout --signal=KILL -k 5 20 nice -n 5 stdbuf -oL env -i A=1 python3 - x <<'PY'\nx\nPY",
    "sudo -u root -E python3 - <<'PY'\nx\nPY",
    "bash -eu -o pipefail -s a b <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "ssh -o BatchMode=yes -p 22 -tt h 'T=1 bash -s' <<'R'\npython3 - <<'PY'\nx\nPY\nR",
    "SSH=\"timeout 60 ssh -o A=b ${XOPTS:-} u@$IP\"\n$SSH \"T=$T bash -s\" <<'R' 2>&1 | grep x\n"
    "python3 - <<'PY'\nx\nPY\nR",
])
def test_trusted_owner_masks_the_data_body(command):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    masked = strip_inert_heredoc_bodies(command, nested=True)
    assert "\nx\n" not in masked and masked.count("\n") == command.count("\n"), masked


@pytest.mark.parametrize("command", [
    # su's operand is a USER and its shell reads stdin; env -S hands a string to a shell
    "su python3 - <<'EOF'\nhermes gateway restart\nEOF",
    "env -S 'bash -s' python3 <<'EOF'\nhermes gateway restart\nEOF",
    "sudo -s python3 <<'EOF'\nhermes gateway restart\nEOF",
    # an unquoted outer body expands `$(…)` locally before the remote shell sees it
    "bash <<EOF\npython3 - <<'PY'\n$(hermes gateway restart)\nPY\nEOF",
    # a function header must open the line; other braces/parens keep the opener visible
    "x; q() { python3 - <<'PY'\nhermes gateway restart\nPY\n}",
    # an outer `bash -s` body's own lines are still commands
    "ssh h \"bash -s\" <<'R'\nhermes gateway restart\nR",
    "ssh h \"bash -s\" <<'R'\npython3 - <<'PY'\nprint(1)\nPY\nhermes gateway restart\nR",
    # a function or alias that shadows a consumer/wrapper name is never a data consumer
    "python3() { bash; }\npython3 - <<'PY'\nhermes gateway restart\nPY",
    "python3() { bash; }; q() { python3 - <<'PY'\nhermes gateway restart\nPY\n}; q",
    "function python3 { bash; }\npython3 - <<'PY'\nhermes gateway restart\nPY",
    "timeout() { bash; }\ntimeout 5 python3 - <<'PY'\nhermes gateway restart\nPY",
    "shopt -s expand_aliases; alias python3=bash\npython3 - <<'PY'\nhermes gateway restart\nPY",
    "python3() { bash; }\nssh h \"bash -s\" <<'R'\nhermes gateway restart\nR",
])
def test_shell_consumers_and_outer_bodies_stay_visible(tmp_path, command):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


def test_deeply_nested_heredocs_stay_bounded(tmp_path):
    """Nesting past the bound keeps the innermost body visible (fail closed), and never recurses
    without limit."""
    from cron.lifecycle_guard import scan_gateway_lifecycle

    db = _big_sqlite(tmp_path / "big.db")
    command = f"python3 - <<'PY'\nimport sqlite3\nc = sqlite3.connect(\n    '{db}')\nPY"
    for level in range(6):
        command = f"ssh h{level} \"bash -s\" <<'R{level}'\n{command}\nR{level}"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and str(db) in refusal


@pytest.mark.parametrize("shape", ["wrapper", "nested-in-ssh", "function-args"])
def test_masked_consumer_mentions_are_still_read(tmp_path, shape):
    """A wrapped/nested/function consumer's mentions are still read: a lifecycle script handed to
    os.system() from the masked body still blocks."""
    from cron.lifecycle_guard import scan_gateway_lifecycle

    script = tmp_path / "restart.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    body = f"import os\nos.system('{script}')\n"
    command = _CONSUMER_SHAPES[shape].format(body=body)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("template", [
    "python3 - <<'PY'\nimport sqlite3\nsqlite3.connect('{db}')\nPY",
    "ssh -o BatchMode=yes h \"bash -s\" <<'R'\nsqlite3 {db} 'select 1'\nR",
    "X='import sqlite3; sqlite3.connect(\"{db}\")'\nssh h \"python3 -c '$X'\"",
])
def test_probe_table_plain_rows_stay_allowed(tmp_path, template):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    db = _big_sqlite(tmp_path / "big.db")
    assert scan_gateway_lifecycle(template.format(db=db), cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("make", [
    lambda d: (f"bash {d / 'big.sh'}", "scan cap"),
    lambda d: ("X='hermes gateway restart'; eval \"$X\"", None),
    lambda d: (f"cd {d} && source ./restart.sh", None),
    lambda d: (f"timeout 20 python3 - <<'PY'\nimport os\nos.system('{d / 'restart.sh'}')\nPY", None),
])
def test_probe_table_controls_still_refused(tmp_path, make):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    (tmp_path / "big.sh").write_text("echo hi\n" * 220000, encoding="utf-8")
    (tmp_path / "restart.sh").write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    command, refusal_part = make(tmp_path)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True
    assert (refusal_part in refusal) if refusal_part else refusal is None


# --- interpreter call syntax tokenized as shell is a mention, not a subshell -------------------

@pytest.mark.parametrize("command", [
    # script B's isolated trigger shapes: the Python is shell-tokenized, and
    # `expanduser("~/x")` reads as a word followed by a subshell `("~/x")`
    "REMOTE=$(cat <<'PY'\nimport os\np = os.path.expanduser(\"{db}\")\nPY\n)\n"
    "ssh h \"python3 -c '$REMOTE'\"",
    "ssh h python3 - <<'PY' | sed 's/^/hub /'\nimport os, sqlite3\n"
    "c = sqlite3.connect(os.path.expanduser(\"{db}\"))\nPY",
    "cat > /tmp/x.py <<EOF\nfrom pathlib import Path\nprint(Path('{db}').stat())\nEOF",
])
def test_path_in_interpreter_call_is_a_mention(tmp_path, command):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    db = _big_sqlite(tmp_path / "big.db")
    assert scan_gateway_lifecycle(command.format(db=db), cwd=str(tmp_path)) == (False, None)


def test_call_argument_mentions_are_still_read(tmp_path):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    script = tmp_path / "restart.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    command = f"ssh h python3 - <<'PY' | sed 's/^/x /'\nimport os\nos.system('{script}')\nPY"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("template", [
    "X=$({big})", "diff <({big}) /dev/null", "tee >({big})", "if ({big}); then :; fi",
    "while ({big}); do :; done", "! ({big})", "time ({big})", "arr=({big})",
])
def test_subshell_and_substitution_parens_still_execute(tmp_path, template):
    """`(` only stops meaning \"runs a command\" after an ordinary word: substitutions, keywords
    and assignments keep the executed fail-closed verdict."""
    from cron.lifecycle_guard import scan_gateway_lifecycle

    big = tmp_path / "big.sh"
    big.write_text("echo hi\n" * 220000, encoding="utf-8")
    unsafe, refusal = scan_gateway_lifecycle(template.format(big=big), cwd=str(tmp_path))
    assert unsafe is True and "scan cap" in refusal


# --- the refused scripts, verbatim ---------------------------------------------------------------

@pytest.mark.parametrize("script, db_rel", [
    ("wcomment2.sh", ".hermes/kanban.db"),
    ("core_split_0923.sh", ".hermes/logs/github-quota/audit.sqlite"),
])
def test_refused_scripts_verbatim_are_not_refused(tmp_path, monkeypatch, script, db_rel):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    home = tmp_path / "home"
    _big_sqlite(home / db_rel)
    monkeypatch.setenv("HOME", str(home))  # the scripts' `~/` paths point into tmp_path
    command = f"bash {_FIXTURES / script} 1.2.3.4 t_x f.txt"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
