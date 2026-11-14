"""Review 5888336995 nit 2: aliases the terminal backend's login snapshot defines live outside the
walked text (``alias -p`` + ``shopt -s expand_aliases`` in ``_snapshot_bootstrap_script``), yet
every command runs with them. The guard must hear about them from the backend."""

import json
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments.base_session_env import _alias_names_printf, _split_alias_marker
from tools.terminal_tool_guards import _session_aliases_for_guard, gateway_lifecycle_block

_FN_BODY = "q() { python3 - <<'PY'\nbash -c 'hermes gateway \"restart\"'\nPY\n}\nq\n"


def _big(tmp_path):
    big = tmp_path / "huge.sh"
    big.write_text("# padding\n" * 130000 + 'hermes gateway "restart"\n')
    return big


def test_reviewer_repro_snapshot_time_alias_fails_closed(tmp_path):
    """`alias t='time '` from the snapshot makes `t ( bash huge.sh )` a subshell running it."""
    command = f"t ( bash {_big(tmp_path)} )"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)  # no session
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path),
                                             session_aliases=frozenset({"t"}))
    assert unsafe is True and refusal is not None and "scan cap" in refusal


def test_unknown_session_aliases_fail_closed(tmp_path):
    command = f"t ( bash {_big(tmp_path)} )"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=None)
    assert unsafe is True and refusal is not None and "scan cap" in refusal


@pytest.mark.parametrize("names,blocked", [
    (frozenset(), False), (frozenset({"ll", "gs"}), False),
    (frozenset({"python3"}), True), (frozenset({"timeout"}), True), (frozenset({"bash"}), True),
    (None, True),
])
def test_session_alias_shadowing_a_consumer_keeps_heredoc_bodies_visible(tmp_path, names, blocked):
    """`alias python3=bash` in the snapshot runs a "Python" heredoc body as shell."""
    assert scan_gateway_lifecycle(_FN_BODY, cwd=str(tmp_path), session_aliases=names)[0] is blocked


def test_bootstrap_alias_names_round_trip_through_real_bash():
    marker = "__HERMES_ALIASES_t__"
    script = ("shopt -s expand_aliases\nalias t='time '\nalias ll='ls -l'\n"
              "alias -- -x='echo'\nalias multi='echo a\necho b'\n" + _alias_names_printf(marker))
    out = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-s"], input=script, text=True,
                         capture_output=True, timeout=10, check=True).stdout
    names = _split_alias_marker(out, marker)
    assert names is not None and {"t", "ll", "-x", "multi"} <= names
    assert _split_alias_marker("no marker here", marker) is None
    empty = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-s"],
                           input=_alias_names_printf(marker), text=True, capture_output=True,
                           timeout=10, check=True).stdout
    assert _split_alias_marker(empty, marker) == frozenset()


def test_guard_reads_backend_hook_on_the_class_only():
    class Backend:
        def __init__(self, names):
            self.names = names

        def lifecycle_guard_session_aliases(self):
            return self.names

    class Broken:
        def lifecycle_guard_session_aliases(self):
            raise AttributeError("_snapshot_ready")

    assert _session_aliases_for_guard(Backend(frozenset({"t"}))) == frozenset({"t"})
    assert _session_aliases_for_guard(Backend(None)) is None
    assert _session_aliases_for_guard(Backend({"t"})) is None  # wrong type: unknown
    assert _session_aliases_for_guard(Broken()) is None
    assert _session_aliases_for_guard(SimpleNamespace(cwd="/")) == frozenset()


# --- t_01a1d565699c975b: a benign session alias must not make every call argument executed -------
# The card's repro, verbatim: refused on the MacBook gateway (its login snapshot defines aliases
# such as `ll`) as "`~/.hermes/kanban.db` is a SQLite database open in this gateway process".
_CARD_REPRO = (
    "ssh -o BatchMode=yes -o ConnectTimeout=15 ubuntu@100.104.201.88 'bash -ls' <<'EOF'\n"
    "python3 - <<'PY'\n"
    "import os\n"
    "print(\"nested ssh python heredoc ok:\", "
    "os.path.exists(os.path.expanduser(\"~/.hermes/kanban.db\")))\n"
    "PY\n"
    "EOF"
)
# Ubuntu's stock ~/.bashrc aliases, and a lone `ll` (the corpus's "benign" snapshot).
_STOCK_ALIASES = frozenset({"ls", "grep", "fgrep", "egrep", "ll", "la", "l", "alert"})


@pytest.fixture
def live_kanban_db(tmp_path, monkeypatch):
    """`~/.hermes/kanban.db` held open (tracked) in this process, as the gateway holds its own."""
    from hermes_cli.sqlite_safe_read import connect_tracked

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    conn = connect_tracked(home / ".hermes" / "kanban.db")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    try:
        yield home / ".hermes" / "kanban.db"
    finally:
        conn.close()


@pytest.mark.parametrize("names", [frozenset(), frozenset({"ll"}), _STOCK_ALIASES])
def test_card_repro_is_not_refused_under_benign_session_aliases(tmp_path, live_kanban_db, names):
    """The db path is only an interpreter call argument (`expanduser("…")`): no session alias
    names `expanduser`, so bash cannot turn that `(` into a subshell. A mention, never a refusal."""
    assert scan_gateway_lifecycle(_CARD_REPRO, cwd=str(tmp_path), session_aliases=names) == (False, None)


@pytest.mark.parametrize("command", [
    "t ( bash {db} )",  # the alias is the opener: may be `alias t='time '`
    "ll x ( bash {db} )",  # any session alias word in the opener counts (blank-ending chains)
])
def test_session_led_call_still_fails_closed(tmp_path, live_kanban_db, command):
    """Controls: a `(` led by a word the session holds as an alias stays executed, and a text that
    may define an alias, or unknown names, keep every call argument executed (main refuses all).
    No path here may contain the word "alias": that alone marks the text as alias-defining."""
    assert "alias" not in str(tmp_path).lower()
    text = command.format(db=live_kanban_db)
    # The same words with no session alias in the opener: a call argument, i.e. a mention.
    assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (
        False, None)
    # `frozenset({1})`: a malformed backend answer (non-str names) is unknown names, not none.
    cases: list[tuple[str, Any]] = [
        (text, frozenset({"ll", "t"})), (text, frozenset({1})), (_CARD_REPRO, None),
        ("shopt -s expand_aliases; alias T=' '\n" + _CARD_REPRO, frozenset({"ll"}))]
    for checked, aliases in cases:
        unsafe, refusal = scan_gateway_lifecycle(checked, cwd=str(tmp_path), session_aliases=aliases)
        assert unsafe is True and refusal is not None and "SQLite" in refusal, aliases


def test_alias_led_session_subshell_runs_in_real_bash(tmp_path):
    """Execution proof for the control above: with `ll` a blank-ending snapshot alias, bash runs
    `ll ( … )` as a subshell, so a session alias in the opener must keep the walk fail-closed."""
    out = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-s"],
                         input="shopt -s expand_aliases\nalias ll='time '\nll ( echo ran )\n",
                         text=True, capture_output=True, timeout=10, check=False).stdout
    assert out.strip() == "ran"
    # Without an alias the same words are a syntax error: nothing runs.
    plain = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-s"], input="ll ( echo ran )\n",
                           text=True, capture_output=True, timeout=10, check=False)
    assert "ran" not in plain.stdout and plain.returncode != 0


def test_local_backend_benign_snapshot_alias_allows_card_repro(tmp_path, live_kanban_db, monkeypatch):
    """End to end through the terminal guard with a real snapshot holding `alias ll='ls -l'`."""
    from tools.environments.local import LocalEnvironment

    init_file = tmp_path / "custom-init.sh"
    init_file.write_text("alias ll='ls -l'\n")
    with patch("tools.environments.local._read_terminal_shell_init_config",
               return_value=([str(init_file)], False)):
        env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
        try:
            names = env.lifecycle_guard_session_aliases()
            assert names is not None and "ll" in names
            monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process",
                                lambda: True)
            result = gateway_lifecycle_block(
                command=_CARD_REPRO, env=env, env_type="local",
                cwd=str(tmp_path), workdir=str(tmp_path), session_key="benign-alias-test")
        finally:
            env.cleanup()
    assert result is None


def test_local_backend_reports_snapshot_aliases_and_terminal_guard_refuses(tmp_path, monkeypatch):
    """End to end: a login-file alias reaches the snapshot, the backend reports it, and the
    terminal guard refuses the reviewer's repro (it allowed it before the fix)."""
    from tools.environments.local import LocalEnvironment

    init_file = tmp_path / "custom-init.sh"
    init_file.write_text("alias t='time '\n")
    with patch("tools.environments.local._read_terminal_shell_init_config",
               return_value=([str(init_file)], False)):
        env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
        try:
            names = env.lifecycle_guard_session_aliases()
            assert names is not None and "t" in names
            assert "__HERMES_ALIASES_" not in env.execute("echo hi").get("output", "")
            monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process",
                                lambda: True)
            result = gateway_lifecycle_block(
                command=f"t ( bash {_big(tmp_path)} )", env=env, env_type="local",
                cwd=str(tmp_path), workdir=str(tmp_path), session_key="snapshot-alias-test")
        finally:
            env.cleanup()
    assert result is not None and "scan cap" in json.loads(result)["error"]


# Second instance on the card (MacBook 07:24): a `python3 - <<'PY'` body that only json.load()s a
# spilled tool result with a 148 KB line was refused at the per-line scan cap. Same root cause: a
# benign session alias made the open() argument an executed script.
@pytest.mark.parametrize("in_ssh", [False, True])
def test_heredoc_body_reading_an_over_cap_data_file_is_not_refused(tmp_path, in_ssh):
    spill = tmp_path / "toolu_013aj8Q6jjdUWMQcjrcZZJSX.txt"
    spill.write_text('{"a": "' + "x" * 148396 + '"}\n')
    body = f"python3 - <<'PY'\nimport json\nd = json.load(open('{spill}'))\nprint(len(d))\nPY\n"
    command = ("ssh -o BatchMode=yes ubuntu@h 'bash -ls' <<'EOF'\n" + body + "EOF") if in_ssh else body
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=_STOCK_ALIASES) == (
        False, None)
    # Control: unknown session aliases still fail closed on the same text.
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=None)
    assert unsafe is True and refusal is not None and "scan cap" in refusal


# --- PR #2358 review 5894325109 nit 1: a nested login/interactive shell holds its own aliases -----
# Its startup files (~/.bash_profile, ~/.zshenv, a remote host's) define them, so the outer
# session's names say nothing about them: inside, every `NAME (` may be a keyword-led subshell.
_NESTED_FOREIGN_SHELLS = [
    "bash -l <<'EOF'\nt ( bash {db} )\nEOF",  # the nit's exact shape
    "bash -i <<'EOF'\nt ( bash {db} )\nEOF",
    "bash -ls <<'EOF'\nt ( bash {db} )\nEOF",
    "bash --login <<'EOF'\nt ( bash {db} )\nEOF",
    "sh -l <<'EOF'\nt ( bash {db} )\nEOF",
    "zsh <<'EOF'\nt ( bash {db} )\nEOF",  # zsh sources ~/.zshenv even when non-interactive
    "ssh -o BatchMode=yes ubuntu@h 'bash -ls' <<'EOF'\nt ( bash {db} )\nEOF",
    "ssh ubuntu@h <<'EOF'\nt ( bash {db} )\nEOF",  # no remote command: a remote login shell
    "ssh ubuntu@h 'bash -s' <<'EOF'\nt ( bash {db} )\nEOF",  # the remote host's startup files
    "sudo -i <<'EOF'\nt ( bash {db} )\nEOF",
    "bash -l -c 't ( bash {db} )'",
    "bash -lc 't ( bash {db} )'",  # a clustered `-c` (#2494)
    "bash -ilc 't ( bash {db} )'",
    "bash -euo pipefail -lc 't ( bash {db} )'",
    "zsh -lc 't ( bash {db} )'",
    "bash -c -l 't ( bash {db} )'",
    "zsh -c 't ( bash {db} )'",
    "zsh -ec 't ( bash {db} )'",
    "bash -l <<'EOF'\npython3 - <<'PY'\nprint(1)\nPY\nt ( bash {db} )\nEOF",
    "bash -l {script}",
    "bash -l <<< 't ( bash {db} )'",
    "bash -l < {script}",
    "bash -l 0< {script}",
    "bash -l 0<<< 't ( bash {db} )'",
    "bash -l <> {script}",
    "runuser -c 't ( bash {db} )' bob",
    "runuser --command='t ( bash {db} )' bob",
    "su -c 't ( bash {db} )' bob",
    "doas -u bob -s <<'EOF'\nt ( bash {db} )\nEOF",
    "env BASH_ENV=x bash -c 't ( bash {db} )'",
    "BASH_ENV=x bash {script}",
    "runuser -l bob <<'EOF'\nt ( bash {db} )\nEOF",
    "doas -s <<'EOF'\nt ( bash {db} )\nEOF",
    "su --command='t ( bash {db} )' bob",
    "BASH_ENV=x bash -c 't ( bash {db} )'",
    "BASH_ENV=x bash <<< 't ( bash {db} )'",
    "BASH_ENV=x bash < {script}",
    "ENV=x sh -i -c 't ( bash {db} )'",
    # su-mode operands after the user, even past `--`, reach the shell (`bash -c CMD`).
    "su -- bob -c 't ( bash {db} )'",
    "runuser -- bob -c 't ( bash {db} )'",
    "su bob -c 't ( bash {db} )'",
    "su -lc 't ( bash {db} )' bob",
    "su -c't ( bash {db} )' bob",
    # getopt_long accepts unique long-option prefixes.
    "runuser --whitelist -u bob <<< 't ( bash {db} )'",
    "runuser --white -u bob <<< 't ( bash {db} )'",
    "runuser --supp -u bob <<< 't ( bash {db} )'",
    "runuser --gro -u bob <<< 't ( bash {db} )'",
    "runuser --comm='t ( bash {db} )' bob",
    "runuser --com 't ( bash {db} )' bob",
    "su --sess='t ( bash {db} )' bob",
    "doas -a style -s <<< 't ( bash {db} )'",
    # A stdin file fed to a login-shell launcher is that shell's script (review 5903452583 B1).
    "su -l bob < {script}",
    "su - bob < {script}",
    "su bob < {script}",
    "su -l bob 0< {script}",
    "su -l bob <> {script}",
    "runuser -l bob < {script}",
    "runuser - bob < {script}",
    "doas -s < {script}",
    "doas -u bob -s < {script}",
    "sudo -i < {script}",
    "sudo -s < {script}",
    "sudo -u bob -i < {script}",
    "ssh host < {script}",
    "ssh -p 22 host bash -s < {script}",
    # A compound command's stdin redirection or heredoc reaches the login shell inside it
    # (PR #2911 review follow-up). Pre-existing: base walked none of these in nested scope.
    "{{ su -l bob; }} < {script}",
    "( su -l bob ) < {script}",
    "{{ su -l bob; }} <<< 't ( bash {db} )'",
    "( su -l bob ) <<< 't ( bash {db} )'",
    "{{ su -l bob; }} <<'EOF'\nt ( bash {db} )\nEOF",
    "{{ su -l bob; }} <<-EOF\n\tt ( bash {db} )\n\tEOF",
    "if true; then su -l bob; fi < {script}",
    "if true; then su -l bob; fi <<'EOF'\nt ( bash {db} )\nEOF",
    "while read l; do su -l bob; done < {script}",
    "while read l; do su -l bob; done <<'EOF'\nt ( bash {db} )\nEOF",
    "for x in 1; do su -l bob; done < {script}",
    "case x in x) su -l bob;; esac < {script}",
    "{{ su -l bob; }} 0< {script}",
    "{{ su -l bob; }} <> {script}",
    "{{ true; {{ su -l bob; }}; }} < {script}",
    "{{\nsu -l bob\n}} < {script}",  # the group opens lines before its redirection
    "{{\nsu -l bob\n}} <<'EOF'\nt ( bash {db} )\nEOF",
    "{{ bash -l; }} < {script}",
    "{{ sudo -i; }} < {script}",
    "{{ doas -s; }} < {script}",
    "{{ ssh host; }} < {script}",
    "{{ su -l bob < /dev/null; }} < {script}",  # fails closed: own redirections are not tracked
    # A file piped into the login shell (t_01a1dae7a625be0c): base walked none of these in
    # nested scope, and handed a piped file to `su`/`sudo`/`ssh` in no scope at all.
    "cat {script} | su -l bob",
    "cat {script} | {{ su -l bob; }}",
    "cat {script} | ( su -l bob )",
    "cat {script} |& su -l bob",
    "cat {script} |\nsu -l bob",
    "cat < {script} | su -l bob",
    "cat {script} | bash -l",
    "cat {script} | sudo -i",
    "cat {script} | ssh host",
    "cat {script} | {{\nsu -l bob\n}}",
    "cat {script} | while read l; do\nsu -l bob\ndone",
    "(\nsu -l bob\n) < {script}",
    "while true; do\nsu -l bob\ndone < {script}",
]


@pytest.mark.parametrize("shape", _NESTED_FOREIGN_SHELLS)
def test_nested_login_shell_body_treats_names_as_unknown(tmp_path, live_kanban_db, shape):
    """Base refused these under any session alias; #2358 allowed them when the outer names
    (`zz`) missed `t`. The inner shell's own startup files decide whether `t` is an alias."""
    assert "alias" not in str(tmp_path).lower()
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(db=live_kanban_db, script=script)
    for names in (frozenset({"zz"}), frozenset(), None):
        unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names)
        assert unsafe is True and refusal is not None and "SQLite" in refusal, (shape, names)


@pytest.mark.parametrize("shape", [
    "bash <<'EOF'\nt ( bash {db} )\nEOF",  # a non-login, non-interactive child inherits no aliases
    "bash -s <<'EOF'\nt ( bash {db} )\nEOF",
    "t2 ( bash {db} )",
    "bash <<< 't ( bash {db} )'",
    "bash < {script}",
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
_PY_PRINTS = "print('t ( bash {db} )')\n"


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
                  "timeout 9 bash --login <<'E'", "exec -l bash <<'E'", None):
        assert _feeds_nested_shell(owner), owner
    for owner in ("bash <<'E'", "bash -s <<'E'", "sh -e <<'E'", "python3 - <<'E'",
                  "ssh h python3 - <<'E'", "ssh h 'cat > f' <<'E'", "bash -l x.sh <<'E'",
                  "sudo -u bob python3 - <<'E'", "cat <<'E'"):
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
