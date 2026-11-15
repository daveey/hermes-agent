"""A pipe into a compound command feeds every command inside it that reads stdin
(t_01a1e0990275c62b): `printf T | { echo x; bash; }` runs T, yet the sink regex's pipe alternative
stopped at the first `;`. Under unknown alias names the nested printed-pipeline walk is skipped
(the session walk is trusted to read the text), so `… | { echo x; bash -l; }; echo $'y'` was
missed too: the session walk never saw a sink."""

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell


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


def _scan(text, tmp_path):
    return scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))


@pytest.mark.parametrize("command", [
    # controls (refused before the fix too)
    "printf 'bash {db}' | bash",
    "printf 't ( bash {db} )' | {{ echo x; bash -l; }}",
    # card repros
    "printf 'bash {db}' | {{ echo x; bash; }}",
    "printf 't ( bash {db} )' | {{ echo x; bash -l; }}; echo $'y'",
    "printf 't ( bash {db} )' | ( echo x; bash -l ); echo $'y'",
    # same gap, other spellings
    "printf 't ( bash {db} )' | {{ echo x; bash -l; }}; alias q=r",
    "printf 'bash {db}' | {{ echo x; bash; }}; echo $'y'",
    "printf 'bash {db}' | ( echo x; bash )",
    "printf 'bash {db}' | {{ echo x\nbash\n}}",
    "printf ':\\nbash {db}\\n' | while read -r l; do bash; done",
    "python3 - <<'PY' | {{ echo x; bash; }}\nprint('bash {db}')\nPY\n",
])
def test_printed_text_piped_into_a_grouped_shell_is_walked(tmp_path, live_kanban_db, command):
    unsafe, refusal = _scan(command.format(db=live_kanban_db), tmp_path)
    assert unsafe is True and refusal is not None


@pytest.mark.parametrize("command", [
    "printf ':' | { echo x; bash; }",
    "printf ':' | { echo x; bash -l; }; echo $'y'",
    "printf ':' | ( echo x; bash -l ); echo $'y'",
    "printf 't ( echo hi )' | { echo x; bash -l; }; echo $'y'",
    "printf 'bash {db}' | {{ echo x; cat; }}",  # no shell reads the pipe
    "printf 'bash {db}' | (cat); echo bash",  # the shell is past the group: not piped
])
def test_clean_twins_stay_allowed(tmp_path, live_kanban_db, command):
    assert _scan(command.replace("{{", "{").replace("}}", "}").replace("{db}", str(live_kanban_db)),
                 tmp_path) == (False, None)


@pytest.mark.parametrize("command,reaches", [
    ("printf T | { echo x; bash; }", True),
    ("printf T | ( :; sh -l )", True),
    ("printf T | while read -r l; do bash; done", True),
    ("printf T | { echo x\nbash\n}", True),
    ("printf T | { echo x; cat; }", False),
    ("printf T | (cat); bash x.sh", False),
    ("printf T | cat; { echo x; bash; }", False),
    ("{ echo x; bash; }", False),
])
def test_output_reaches_shell_follows_the_pipe_into_the_group(command, reaches):
    assert output_reaches_shell(command) is reaches


def test_real_bash_runs_text_piped_into_a_grouped_shell():
    """The premise: the group's shell does execute the piped text."""
    out = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c",
         "printf 'echo FED\\n' | { echo x; bash --noprofile --norc; }; echo $'y'"],
        text=True, capture_output=True, timeout=10, check=True)
    assert out.stdout.split() == ["x", "FED", "y"] and out.stderr == ""
