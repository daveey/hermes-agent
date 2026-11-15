"""The referenced-script walk sees the same inert-heredoc-masked view as the direct scan (#110422).

``_direct_lifecycle_scan`` masks provably-inert heredoc bodies (quoted delimiter, allowlisted
data consumer such as ``python3 - <<'PY'``), but ``_contains_unsafe_gateway_action`` walked
referenced scripts and ``sh -c`` payloads on the raw command, so a >1 MiB data path mentioned
inside the Python body failed closed as an oversized "script".
"""

import shutil
import sys
from pathlib import Path

import pytest

from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script as guard
from tests.cron import heredoc_shape_corpus as proof


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
    if "${XOPTS:-}" in command:
        # This old positive case was unsound: unknown options can inject a remote
        # program. Retain the case with its fail-closed expectation (real shell below).
        assert masked == command
    else:
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


# --- binding provenance: textual assignments are not necessarily shell state --------------------

@pytest.mark.parametrize("decoy", [
    "if false; then CMD='bash -s'; fi",
    "# CMD='bash -s'",
    "CMD='bash -s' true",
    "echo CMD='bash -s'",
])
def test_untrusted_variable_assignment_does_not_hide_executed_body(tmp_path, decoy):
    import os
    import subprocess
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    marker = tmp_path / "marker"
    hermes = tmp_path / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    hermes.chmod(0o755)
    command = (
        "transform() { sed s/python3/bash/ | bash; }\nCMD=transform\n" + decoy + "\n"
        "$CMD <<'R'\npython3 - <<'PY'\n"
        "bash -c 'hermes gateway \"restart\"'\nPY\nR\n"
    )
    result = subprocess.run([proof.proof_bash()], input=proof.TIMEOUT_PREAMBLE + command,
                            text=True, encoding="utf-8", capture_output=True, check=False,
                            env={**os.environ, "PATH": proof.proof_path(tmp_path),
                                 "MARKER": str(marker)}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "called"
    assert guard(command, cwd=str(tmp_path)) is True
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("mutation", [
    "set -- ev al\n\"$1$2\" 'CMD=transform'",
    "set -- pri ntf -v CMD transform\n\"$1$2\" \"$3\" \"$4\" \"$5\"",
])
def test_synthesized_mutator_cannot_change_a_trusted_shell_binding(tmp_path, mutation):
    import os
    import subprocess
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    marker = tmp_path / "marker"
    hermes = tmp_path / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    hermes.chmod(0o755)
    transform = tmp_path / "transform"
    transform.write_text('#!/bin/sh\nsed s/python3/bash/ | bash\n')
    transform.chmod(0o755)
    command = ("CMD=bash\n" + mutation + "\n$CMD <<'R'\npython3 - <<'PY'\n"
               "bash -c 'hermes gateway \"restart\"'\nPY\nR\n")
    result = subprocess.run([proof.proof_bash()], input=proof.TIMEOUT_PREAMBLE + command,
                            text=True, encoding="utf-8", capture_output=True, check=False,
                            env={**os.environ, "PATH": proof.proof_path(tmp_path),
                                 "MARKER": str(marker)}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "called"
    assert guard(command, cwd=str(tmp_path)) is True
    assert strip_inert_heredoc_bodies(command, nested=True) == command


def test_ssh_binding_with_unknown_expansions_can_change_remote_program(tmp_path):
    import os
    import subprocess
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    marker = tmp_path / "marker"
    hermes = tmp_path / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    hermes.chmod(0o755)
    ssh = tmp_path / "ssh"
    # SSH's documented joining of remote arguments, without any network access.
    ssh.write_text('#!/bin/sh\nshift 3\nexec sh -c "$*"\n')
    ssh.chmod(0o755)
    command = ('SSH="timeout 60 ssh -o A=b ${XOPTS:-} u@$IP"\n'
               '$SSH "T=$T bash -s" <<\'R\'\npython3 - <<\'PY\'\n'
               'bash -c \'hermes gateway "restart"\'\nPY\nR\n')
    result = subprocess.run([proof.proof_bash()], input=proof.TIMEOUT_PREAMBLE + command,
                            text=True, encoding="utf-8", capture_output=True, check=False,
                            env={**os.environ, "PATH": proof.proof_path(tmp_path),
                                 "MARKER": str(marker), "IP": "h", "T": "x",
                                 "XOPTS": "otherhost sed s/python3/bash/ | bash #"}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "called"
    assert guard(command, cwd=str(tmp_path)) is True
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("prefix", [
    "if false; then\nCMD='bash -s'\nfi\n",
    "# CMD='bash -s'\n",
    "CMD='bash -s' true\n",
    "echo CMD='bash -s'\n",
    "f() {\nCMD='bash -s'\n}\n",
    "true && (\nCMD='bash -s'\n)\n",
    "echo '\nCMD=bash\n'\n",
    "false &&\nCMD=bash\n",
    "CMD=bash\nCMD=bash\n",
    "CMD=bash\nprintf -v CMD transform\n",
    "CMD=bash\ne'val' 'CMD=transform'; :\n",
    "CMD=bash\nread CMD\n",
    "CMD=bash\nunknown_command\n",
    "CMD=bash\nset -- ev al\n\"$1$2\" 'CMD=transform'\n",
    "CMD=bash\nset -- printf -v CMD transform\n\"$@\"\n",
    "CMD='$OTHER'\n",
    "CMD=bash\nPATH=/untrusted\n",
    "CMD= bash\n",
    "CMD= 'bash -s'\n",
])
def test_variable_binding_requires_unique_standalone_top_level_assignment(prefix):
    from tools.shell_heredoc import _variable_value

    assert _variable_value(prefix, "CMD", len(prefix)) is None


@pytest.mark.parametrize("prefix", ["CMD='bash -s'\n", 'CMD="bash -s"\n'])
def test_variable_binding_accepts_unique_standalone_assignment(prefix):
    from tools.shell_heredoc import _variable_value

    assert _variable_value(prefix, "CMD", len(prefix)) == "bash -s"


# --- stdin scripts may be consumed by earlier commands before the shell parses them -------------

@pytest.mark.parametrize("unit", [
    "", "# comment", "MESSAGE='hello world'", 'echo "$MESSAGE"',
    'echo before; q "$1"', ": ready", "set -u",
])
def test_nonreading_units_use_a_generic_positive_grammar(unit):
    from tools.shell_heredoc import _nonreading_shell_unit

    assert _nonreading_shell_unit(unit, {"q"}) is True


@pytest.mark.parametrize("unit", [
    "arbitrary_cli comment", "PATH=/untrusted", "set -- eval", "printf -v PATH /untrusted",
    "printf '%n' PATH", 'echo "${X:=changed}"', 'echo "$((X=1))"',
    "echo safe </dev/stdin", r"e\cho safe",
    'echo "$(cat /arbitrary/file)"', "cat /dev/stdin", "q | bash",
    "HB=$(command -v hermes || ls ~/.local/bin/hermes 2>/dev/null || echo ~/.hermes/hermes-agent/venv/bin/hermes)",
    'env -u X timeout 60 "$HB" kanban comment x "$(cat /tmp/wc_$T.txt)" 2>&1 | tail -3 | cut -c1-200',
])
def test_cli_and_file_substitutions_are_not_proven_nonreading(unit):
    from tools.shell_heredoc import _nonreading_shell_unit

    # An executable discovered on PATH may read stdin; a file can be a symlink to it.
    assert _nonreading_shell_unit(unit, set()) is False


@pytest.mark.parametrize("prefix", [
    "sed 's/^python3 - /bash -s /' | bash",
    "while IFS= read -r line; do eval \"$line\"; done",
    "hash -p /bin/bash python3",
])
def test_nested_stdin_hijack_walk_only_body_stays_visible(tmp_path, prefix):
    import os
    import subprocess
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    marker = tmp_path / "marker"
    hermes = tmp_path / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    hermes.chmod(0o755)
    command = ("bash -s <<'R'\n" + prefix + "\npython3 - <<'PY'\n"
               "bash -c 'hermes gateway \"restart\"'\nPY\nR\n")
    result = subprocess.run([proof.proof_bash()], input=proof.TIMEOUT_PREAMBLE + command,
                            text=True, encoding="utf-8", capture_output=True, check=False,
                            env={**os.environ, "PATH": proof.proof_path(tmp_path),
                                 "MARKER": str(marker)}, timeout=10)
    assert marker.read_text() == "called", result.stderr
    assert guard(command, cwd=str(tmp_path)) is True
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("opener", [
    "ssh ${XOPTS:-} h 'bash -s' <<'R'",
    "bash -s <<R",
])
def test_nested_masking_requires_literal_owner_and_quoted_outer_body(opener):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = opener + "\npython3 - <<'PY'\nx\nPY\nR\n"
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("prefix", [
    "unknown_command; ",
    "unknown_command <<'A'\ndata\nA\n",
])
def test_nested_masking_stops_after_untrusted_same_line_or_heredoc_owner(prefix):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = "bash -s <<'R'\n" + prefix + "python3 - <<'PY'\nx\nPY\nR\n"
    assert strip_inert_heredoc_bodies(command, nested=True) == command


def test_nested_function_second_body_is_not_assumed_nonreading():
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = ("bash -s <<'R'\nf() { python3 - <<'ONE'\nfirst\nONE\n"
               "python3 - <<'TWO'\nsecond\nTWO\n}\nR\n")
    masked = strip_inert_heredoc_bodies(command, nested=True)
    assert "\nfirst\n" not in masked
    assert "\nsecond\n" in masked
    assert masked.count("\n") == command.count("\n")


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


# Independent review: prove execution with a fake hermes, never a real gateway.
def _run_bash_marker(tmp_path, command, prefix=()):
    """Run `command` in real Bash (under argv *prefix*) with a fake `hermes`; return
    (marker written, stderr)."""
    import os
    import subprocess

    marker = tmp_path / "marker"
    marker.unlink(missing_ok=True)
    hermes = tmp_path / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    hermes.chmod(0o755)
    result = subprocess.run(
        [*prefix, proof.proof_bash()], input=proof.TIMEOUT_PREAMBLE + command,
        text=True, encoding="utf-8", capture_output=True, timeout=10,
        check=False, env={**os.environ, "PATH": proof.proof_path(tmp_path), "FAKE_BIN": str(tmp_path),
                          "MARKER": str(marker), "BASH_ENV": "", "ENV": ""}, cwd=tmp_path,
    )
    return marker.exists() and marker.read_text() == "called", result.stderr


def _startup_shell_prefix_or_skip(request):
    """Argv prefix under which a login bash sources the test's `$HOME` startup files on this host
    (none, or bubblewrap masking the system /etc/profile*). Without one the execution proof is
    impossible here: a real skip (the runner counts it) that also names the test in
    $GITHUB_STEP_SUMMARY, since the runner drops skip reasons and warnings."""
    from tests.cron import heredoc_shape_corpus as corpus

    prefix, how = corpus.startup_shell_prefix()
    if prefix is None:
        reason = f"no shell on this host sources $HOME startup files ({how})"
        corpus.note_ci_skip(request.node.nodeid, reason)
        pytest.skip(reason)
    return prefix


def _assert_bash_marker(tmp_path, command):
    called, stderr = _run_bash_marker(tmp_path, command)
    assert called, stderr


@pytest.mark.parametrize("depth", [0, 1])
@pytest.mark.parametrize("mutation", ["CMD=transform;", "CMD=transform &&", "read CMD <<<transform;"])
def test_same_line_binding_mutation_keeps_transformed_body_visible(tmp_path, depth, mutation):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    transform = tmp_path / "transform"
    transform.write_text('#!/bin/sh\nsed s/python3/bash/ | bash\n')
    transform.chmod(0o755)
    command = ("CMD='bash -s'\n" + mutation + " $CMD <<'R'\npython3 - <<'PY'\n"
               "bash -c 'hermes gateway \"restart\"'\nPY\nR\n")
    if depth:
        command = "bash -s <<'OUTER'\n" + command + "OUTER\n"
    _assert_bash_marker(tmp_path, command)
    assert strip_inert_heredoc_bodies(command, nested=True) == command
    assert guard(command, cwd=str(tmp_path)) is True


@pytest.mark.parametrize("shape", ["assignment", "env", "export", "export-heredoc", "remote", "in-body"])
def test_shell_startup_environment_cannot_authorize_nested_masking(tmp_path, shape):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    startup = tmp_path / "startup"
    # Clear BASH_ENV for the replacement shell: only the target shell's stdin is rewritten.
    startup.write_text("unset BASH_ENV\nexec 0< <(sed s/python3/bash/)\n")
    body = "python3 - <<'PY'\nbash -c 'hermes gateway \"restart\"'\nPY\n"
    ssh = tmp_path / "ssh"
    # Local SSH transport control; no network access.
    ssh.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$*"\n')
    ssh.chmod(0o755)
    opener = {
        "env": f"env BASH_ENV={startup} bash -s",
        "export": f"export BASH_ENV={startup}\nbash -s",
        "export-heredoc": f"source /dev/stdin <<'INIT'\nexport BASH_ENV={startup}\nINIT\nbash -s",
        "remote": f"ssh h 'BASH_ENV={startup} bash -s'",
    }.get(shape, f"BASH_ENV={startup} bash -s")
    command = opener + " <<'R'\n" + body + "R\n"
    if shape == "in-body":
        command = "bash -s <<'OUTER'\n" + command + "OUTER\n"
    _assert_bash_marker(tmp_path, command)
    assert strip_inert_heredoc_bodies(command, nested=True) == command
    assert guard(command, cwd=str(tmp_path)) is True


@pytest.mark.parametrize("name", ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PATH", "BASH_FUNC_python3%%"])
@pytest.mark.parametrize("wrapper", ["", "env ", "sudo "])
def test_reserved_environment_assignments_are_not_transparent(name, wrapper):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = wrapper + name + "=/untrusted bash -s <<'R'\npython3 - <<'PY'\nx\nPY\nR\n"
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("prefix", [
    "eval 'python3() { command bash; }'; export -f python3",
    "eval 'python3() { command bash; }'; declare -fx python3",
    "export PATH=EVIL:$PATH",
])
def test_outer_prefix_must_preserve_nested_interpreter_identity(tmp_path, prefix):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    evil = tmp_path / "evil"
    evil.mkdir()
    python = evil / "python3"
    python.write_text('#!/bin/sh\nexec /bin/bash "$@"\n')
    python.chmod(0o755)
    command = (prefix.replace("EVIL", str(evil)) + "\nbash -s <<'R'\npython3 - <<'PY'\n"
               "bash -c 'hermes gateway \"restart\"'\nPY\nR\n")
    _assert_bash_marker(tmp_path, command)
    assert strip_inert_heredoc_bodies(command, nested=True) == command
    assert guard(command, cwd=str(tmp_path)) is True


@pytest.mark.parametrize("prefix", ["time -p", "time --", "time -p --", "coproc worker", "! time -p", "if time -p"])
def test_keyword_operands_before_parens_still_execute_scripts(tmp_path, prefix):
    from cron.lifecycle_guard import scan_gateway_lifecycle

    big = tmp_path / "big.sh"
    big.write_text("# padding\n" * 130000 + 'hermes gateway "restart"\n')
    command = f"{prefix} ( bash {big} )"
    command += "; then :; fi" if prefix.startswith("if ") else "; wait"
    called, stderr = _run_bash_marker(tmp_path, command)
    # Bash 3.2 supports only the bare `time -p` spelling in this set.
    expected = proof.proof_bash_version() >= (4, 0) or prefix == "time -p"
    assert called is expected, stderr
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and "scan cap" in refusal


# A system /etc/profile may reset PATH (Debian's does); put the fake-hermes dir back first.
# The startup file records that it ran (and the HOME it saw), so a host whose system login
# files keep bash from sourcing `$HOME` startup files is told apart from a failed rewrite.
_REWRITE_STARTUP = (
    'printf "ran HOME=%s\\n" "$HOME" >> "$FAKE_BIN/startup-ran"\n'
    'PATH="$FAKE_BIN:$PATH"\nunset BASH_ENV\nexec 0< <(sed s/python3/bash/)\n'
)
_WALK_ONLY_BODY = "python3 - <<'PY'\nbash -c 'hermes gateway \"restart\"'\nPY\n"


@pytest.mark.parametrize("shape", [
    "home-login", "home-long-login", "home-interactive", "env-home-login", "separate-line-home",
    "prompt-command", "combined-flags", "nested", "remote",
])
def test_startup_files_and_prompt_hooks_cannot_authorize_nested_masking(tmp_path, shape, request):
    """Review 5882059941 #1: login/interactive shells source `$HOME` startup files and run
    PROMPT_COMMAND before each line; either can rewrite the stdin script (main refuses).
    Review 5888336995 nit 1: the execution proof runs wherever some shell on the host sources
    `$HOME` startup files (CI: bubblewrap masking the runner's /etc/profile*), else it skips."""
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    home = tmp_path / "home"
    home.mkdir()
    for name in (".bash_profile", ".profile", ".bashrc"):
        (home / name).write_text(_REWRITE_STARTUP)
    ssh = tmp_path / "ssh"
    # Local SSH transport control; no network access.
    ssh.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$*"\n')
    ssh.chmod(0o755)
    opener = {
        "home-login": f"HOME={home} bash -l -s",
        "home-long-login": f"HOME={home} bash --login -s",
        "home-interactive": f"HOME={home} bash -i -s",
        "env-home-login": f"env HOME={home} bash -l",
        "separate-line-home": f"HOME={home}\nbash -l -s",
        "prompt-command": "PROMPT_COMMAND='exec 0< <(sed s/python3/bash/)' bash --norc -i -s",
        "combined-flags": f"HOME={home} bash -els",
        "nested": f"HOME={home} bash -l -s",
        "remote": f"ssh h 'HOME={home} bash -l -s'",
    }[shape]
    command = opener + " <<'R'\n" + _WALK_ONLY_BODY + "R\n"
    if shape == "nested":
        command = "bash -s <<'OUTER'\n" + command + "OUTER\n"
    # The guard verdicts hold on every host; only the execution proof below may skip.
    assert strip_inert_heredoc_bodies(command, nested=True) == command
    assert guard(command, cwd=str(tmp_path)) is True
    prefix = () if shape == "prompt-command" else _startup_shell_prefix_or_skip(request)
    startup_ran = tmp_path / "startup-ran"
    called, stderr = _run_bash_marker(tmp_path, command, prefix)
    ran = startup_ran.read_text() if startup_ran.exists() else ""
    assert called, f"startup file: {ran!r}\n{stderr}"


@pytest.mark.parametrize("opener", [
    "zsh -s", "HOME=/untrusted zsh -s", "ZDOTDIR=/untrusted zsh", "ssh h 'zsh -s'",
    "bash -i", "bash -l", "bash --login -s", "sh -il", "bash -H -s", "bash -k -s",
    "HOME=/untrusted bash -s", "PROMPT_COMMAND=x bash -s", "PS1=x bash -s", "env PS0=x bash -s",
])
def test_startup_reading_shells_are_not_verbatim_stdin_shells(opener):
    """zsh sources `$ZDOTDIR/.zshenv` on every start; -i/-l/-H/-k change how lines are read."""
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = opener + " <<'R'\npython3 - <<'PY'\nx\nPY\nR\n"
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("opener", [
    "bash -s", "bash -e -s", "sh -eu", "bash --norc --noprofile -s",
    "bash -s -o pipefail", "bash -o errexit -s -u", "bash -s -- -i", "T=1 bash -s x -i -l",
    "ssh h 'bash -s -o pipefail'",
])
def test_plain_stdin_shells_still_nest_mask(opener):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = opener + " <<'R'\npython3 - <<'PY'\nx\nPY\nR\n"
    assert strip_inert_heredoc_bodies(command, nested=True) != command


@pytest.mark.parametrize("opener", [
    "bash -s -i", "bash -s -l", "bash -s --login", "bash -s -H", "bash -s -k", "bash -es -il",
    "bash -o history -o histexpand -s", "bash -s -o history -o histexpand", "bash -s -H -o history",
    "bash -s -o keyword", "bash -s +o interactive-comments", "bash -s -o vi", "bash -s -o",
    "bash -s -O expand_aliases", "bash -s -", "ssh h 'bash -o history -o histexpand -s'",
    "ssh h 'bash -s -i'",
])
def test_options_after_dash_s_are_still_validated(opener):
    """Review 5887281618: bash keeps parsing options after `-s`, so `-s` must not end the check,
    and `-o`/`+o` operands are allowlisted (main refuses these shapes)."""
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    command = opener + " <<'R'\npython3 - <<'PY'\nx\nPY\nR\n"
    assert strip_inert_heredoc_bodies(command, nested=True) == command


@pytest.mark.parametrize("opener", [
    "bash -o history -o histexpand -s", "bash -s -o history -o histexpand", "bash -s -H -o history",
    "ssh h 'bash -o history -o histexpand -s'",
])
def test_history_expansion_after_dash_s_runs_walk_only_body(tmp_path, opener):
    """Review 5887281618: with history expansion on, `timeout !!:1-3 python3 -` runs
    `timeout 5 bash -s python3 -`, so the inner body is shell, not Python. Real-Bash proof."""
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    ssh = tmp_path / "ssh"
    # Local SSH transport control; no network access.
    ssh.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$*"\n')
    ssh.chmod(0o755)
    command = (opener + " <<'R'\n: 5 bash -s\ntimeout !!:1-3 python3 - <<'PY'\n"
               "bash -c 'hermes gateway \"restart\"'\nPY\nR\n")
    called, stderr = _run_bash_marker(tmp_path, command)
    # Bash 3.2 does not expand history in this noninteractive stdin form.
    assert called is (proof.proof_bash_version() >= (4, 0)), stderr
    assert strip_inert_heredoc_bodies(command, nested=True) == command
    assert guard(command, cwd=str(tmp_path)) is True


def _keyword_led_subshell_command(tmp_path, shape):
    big = tmp_path / "big.sh"
    big.write_text("# padding\n" * 130000 + 'hermes gateway "restart"\n')
    # No file or directory name may contain "alias": only the command text may enable aliases.
    prefix = tmp_path / "prefix.sh"
    prefix.write_text("shopt -s expand_aliases\nalias T=' '\n")
    return {
        "function": f"function f ( bash {big} ); f",
        "alias-blank": f"shopt -s expand_aliases\nalias T=' '\nT ( bash {big} )",
        "alias-time": f"shopt -s expand_aliases\nalias T=time\nT ( bash {big} )",
        "alias-quote-split": f"shopt -s expand_al''iases\nal''ias T=' '\nT ( bash {big} )",
        "alias-sourced": f"source {prefix}\nT ( bash {big} )",
    }[shape]


@pytest.mark.parametrize("shape", [
    "function", "alias-blank", "alias-time", "alias-quote-split", "alias-sourced",
])
def test_keyword_led_subshells_still_execute_scripts(tmp_path, shape):
    """Review 5882059941 #2: `function f ( … )` and alias-expanded keyword prefixes make `NAME (`
    a subshell; the oversized-script refusal must survive (main refuses)."""
    from cron.lifecycle_guard import scan_gateway_lifecycle

    assert "alias" not in str(tmp_path).lower()
    command = _keyword_led_subshell_command(tmp_path, shape)
    called, stderr = _run_bash_marker(tmp_path, command)
    expected = proof.proof_bash_version() >= (4, 0) or shape != "function"
    assert called is expected, stderr
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and refusal is not None and "scan cap" in refusal


def test_generated_shape_corpus_never_allows_what_bash_runs_and_main_refuses(tmp_path, request):
    """Card comment 10718 item 2: a seeded sample of the product of shell option words (``-s``
    before, after, bundled or absent) x stdin wrappers x nesting forms x payloads. Real bash runs
    every case; whenever it ran the walk-only payload and origin/main refuses the command (the
    checked-in verdicts), this tree must refuse it too. The six rows of review 5887281618 are in
    the sample, and so is every session-alias case (review 5888336995 nit 2: aliases the
    backend's login snapshot defines outside the walked text). Regenerate the main verdicts with
    the CLI in ``heredoc_shape_corpus``."""
    import itertools
    import time
    import warnings

    from tests.cron import heredoc_shape_corpus as corpus

    fixture, main_refuses = corpus.load_main_verdicts()
    full = corpus.generate_full_corpus()
    assert fixture["corpus_digest"] == corpus.corpus_digest(full), (
        "corpus changed: regenerate main_verdicts.json from origin/main (see heredoc_shape_corpus)")
    cases = corpus.generate_cases()
    for dimension, values in (("wrapper", corpus.WRAPPERS), ("nesting", corpus.NESTINGS),
                              ("shell", corpus.SHELLS), ("payload", tuple(corpus.PAYLOADS))):
        for value in values:
            for position in ("before", "after", "bundled", "none"):
                assert any(getattr(case, dimension) == value and case.s_position == position
                           for case in cases), (dimension, value, position)
    for option in corpus.OPTIONS:
        for position in ("before", "after", "none"):
            assert any(option in case.words and case.s_position == position for case in cases), (
                option, position)
    review = [case for case in cases if case.s_position == "review"]
    assert len(review) == len(corpus.REVIEW_ROWS)
    session = [case for case in cases if case.s_position == "session"]
    assert len(session) == (len(corpus.SESSIONS) * len(corpus.SESSION_FORMS)
                            * len(corpus.SESSION_PAYLOADS))
    # Nit 3: every sink x nesting x consumer x printed line, and every wrapper under each.
    sink = [case for case in cases if case.s_position == "sink"]
    assert {(case.sink, case.nesting, case.payload) for case in sink} == {
        (s, n, f"{c}-{line}") for s in corpus.SINKS for n in corpus.SINK_NESTINGS
        for c in corpus.SINK_CONSUMERS for line in corpus.SINK_LINES}
    assert {(case.sink, case.wrapper, case.nesting) for case in sink} == {
        (s, w, n) for s in corpus.SINKS for w in corpus.WRAPPERS for n in corpus.SINK_NESTINGS}
    # Card t_01a1d5f876488f96: every redirection word at every place x target, under every
    # wrapper and nesting form.
    redirect = [case for case in cases if case.s_position == "redirect"]
    prefixes = (*corpus.REDIRECT_PREFIXES, *corpus.SUBSTITUTION_PREFIXES)
    assert {(case.words[0], case.place, case.payload) for case in redirect} == set(
        itertools.product(prefixes, corpus.REDIRECT_PLACES, corpus.REDIRECT_TARGETS))
    assert {(case.words[0], case.wrapper) for case in redirect} == set(
        itertools.product((*corpus.REDIRECT_PREFIXES, *corpus.SUBSTITUTION_PREFIXES), corpus.WRAPPERS))
    assert {(case.words[0], case.nesting) for case in redirect} == set(
        itertools.product((*corpus.REDIRECT_PREFIXES, *corpus.SUBSTITUTION_PREFIXES), corpus.REDIRECT_NESTINGS))

    started = time.monotonic()
    results = corpus.evaluate(tmp_path, cases)
    elapsed = time.monotonic() - started
    bad = corpus.violations(results, main_refuses)
    assert not bad, "\n".join(case.command for case in bad[:10])
    clean = corpus.false_refusals(results)
    assert not clean, "\n".join(case.command for case in clean[:10])
    ran = {case.key for case, executed, _refused in results if executed}
    allowed = sum(not refused for _case, _executed, refused in results)
    # Non-vacuity premises follow the actual shell; scanner properties above do not.
    # The nit-2 alias-subshell repro executes in every form on both shell versions.
    assert all((case.key in ran) == (proof.proof_bash_version() >= (4, 0))
               for case in review if case.payload == "history")
    assert all(case.key in ran for case in session
               if case.session == "time" and case.payload == "alias-subshell")
    # Keep all cases in the security checks above; only real-execution premises
    # depend on an external wrapper being installed in the restricted proof PATH.
    def wrapper_available(case):
        return not case.wrapper.startswith("stdbuf ") or shutil.which(
            "stdbuf", path=proof.proof_path(tmp_path)) is not None

    # Bash 3.2 on Darwin does not execute a sourced process-substitution fd.
    missing_sinks = [case.command for case in sink if wrapper_available(case) and case.key not in ran
                     and (sys.platform != "darwin" or proof.proof_bash_version() >= (4, 0)
                          or case.sink != ". <(…)")]
    assert not missing_sinks, "\n".join(missing_sinks[:10])
    assert all((case.key in ran) == (case.payload != "clean") for case in redirect
               if case.words[0] not in corpus.REDIRECT_PLAIN_WORDS and wrapper_available(case)
               and (proof.proof_bash_version() >= (4, 1) or not case.words[0].startswith("{")))
    assert all((case.key in ran) == (case.place == "operand" and case.payload == "stdin-s")
               for case in redirect if case.words[0] in corpus.REDIRECT_PLAIN_WORDS
               and wrapper_available(case))
    # Startup-file rows need a shell that sources the test's `$HOME` files (see
    # corpus.startup_shell_prefix): every one of them must run when there is one.
    prefix, how = corpus.startup_shell_prefix()
    missed = [case.command.split("\n", 1)[0] for case in review if case.key not in ran
              and (case.payload != "history" or proof.proof_bash_version() >= (4, 0))]
    warnings.warn(f"shape corpus: {len(cases)} cases in {elapsed:.1f}s, bash ran the payload in "
                  f"{len(ran)}, guard allows {allowed}, 0 violations; startup shell: {how}; "
                  f"bash={proof.proof_bash()} {proof.proof_bash_version()}",
                  stacklevel=1)
    if prefix is None:
        reason = f"review rows without an execution proof on this host: {missed} ({how})"
        corpus.note_ci_skip(request.node.nodeid, reason)
        pytest.skip(reason)
    assert not missed, f"startup shell {how!r} ran but these rows did not: {missed}"


_LIVE_SSH_BODY = (
    "export A=1\n"
    "hermes kanban comment t_x --author default \"$(cat /tmp/f.md)\"\n"
    "python3 - <<'EOF'\nimport sqlite3, os\n"
    "db = sqlite3.connect(\"file:\" + os.path.expanduser(\"~/.hermes/kanban.db\") + \"?mode=ro\", uri=True)\n"
    "EOF\n"
)


@pytest.mark.parametrize("remote", ["bash -l -s", "bash -s"])
def test_live_ssh_body_reading_kanban_db(tmp_path, monkeypatch, remote):
    """Card comment (23:13 PT): main refused this on the MacBook, naming the local kanban.db the
    remote body only reads. Expected verdict here: allowed. The nested python3 body stays visible
    (`bash -l` sources the remote's login files, which may rewrite it, and neither `scp` nor
    `export` is a proven non-reading unit), but `~/.hermes/kanban.db` appears only as a call
    argument (`expanduser("…")`), so it is a mention: read when readable, never refused."""
    from cron.lifecycle_guard import scan_gateway_lifecycle
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    home = tmp_path / "home"
    _big_sqlite(home / ".hermes" / "kanban.db")
    monkeypatch.setenv("HOME", str(home))
    command = (f"scp -q f.md host:/tmp/f.md && timeout 90 ssh -o BatchMode=yes host '{remote}' "
               f"<<'EOS' 2>&1 | grep -av X | cut -c1-220\n{_LIVE_SSH_BODY}EOS\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    assert strip_inert_heredoc_bodies(command, nested=True) == command
