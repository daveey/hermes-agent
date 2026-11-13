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
