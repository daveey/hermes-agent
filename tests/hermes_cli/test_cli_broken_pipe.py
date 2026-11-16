"""A closed output consumer must not produce a CLI traceback at write or exit."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("argument", ["--version", "--help", "kanban-show"])
@pytest.mark.parametrize("unbuffered", [False, True])
def test_cli_to_closed_pipe_is_quiet(tmp_path, unbuffered, argument):
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HERMES_RUNTIME_DIR=str(tmp_path / "runtime"))
    argv = [argument]
    if argument == "kanban-show":
        from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
        path = tmp_path / "fixture.db"
        with kbc.connect_closing(path) as conn:
            task_id = kb.create_task(conn, title="pipe fixture", body="body\n" * 20000)
        env["HERMES_KANBAN_DB"] = str(path)
        argv = ["kanban", "show", task_id]
    command = [sys.executable, *(["-u"] if unbuffered else []), "-m", "hermes_cli.main", *argv]
    complete = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=60, check=False)
    assert complete.returncode == 0, complete.stderr
    assert complete.stdout
    if argument == "kanban-show":
        assert "pipe fixture" in complete.stdout
        assert "body\n" in complete.stdout
    # The read end is gone before startup: deterministic EPIPE, no head timing race.
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(
            [sys.executable, *(["-u"] if unbuffered else []), "-m", "hermes_cli.main", *argv],
            cwd=ROOT, env=env, stdout=writer, stderr=subprocess.PIPE, text=True, timeout=60, check=False,
        )
    finally:
        os.close(writer)
    assert result.returncode == 1
    assert "BrokenPipeError" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr


@pytest.mark.parametrize("error", [BrokenPipeError(32, "internal transport"), OSError(28, "disk full"), ValueError("bad input"), SystemExit(7)])
def test_non_stdout_failures_propagate(error):
    from hermes_cli.stdio_pipe import quiet_stdout_pipe

    original = sys.stdout

    @quiet_stdout_pipe
    def command():
        raise error

    with pytest.raises(type(error)) as caught:
        command()
    assert caught.value is error
    assert sys.stdout is original


def test_normal_output_and_return_are_preserved(capsys):
    from hermes_cli.stdio_pipe import quiet_stdout_pipe

    original = sys.stdout

    @quiet_stdout_pipe
    def command():
        print("complete")
        return 7

    assert command() == 7
    assert sys.stdout is original
    assert capsys.readouterr().out == "complete\n"
