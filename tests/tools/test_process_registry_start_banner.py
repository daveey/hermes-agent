"""What the login shell's rc files print before the command is not the command's output.

``spawn_local`` runs ``$SHELL -lic``, so a host whose startup files print something (Ubuntu's
/etc/bash.bashrc sudo hint for an account without ``~/.sudo_as_admin_successful``, motd, locale
warnings) used to start every background job with those lines, and the CLI dock painted an idle job
as ``last: See "man sudo_root" for details.`` instead of ``starting`` (t_01a1d69d460bc977).
"""

import shlex
import shutil

import pytest

from tests.waiting import wait_until
from tools.process_registry import ProcessRegistry, ProcessSession

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")

BANNER = 'To run a command as administrator (user "root"), use "sudo <command>".\nSee "man sudo_root" for details.'


def _banner_shell(tmp_path, monkeypatch, rc_text):
    """A ``$SHELL`` named bash whose login startup runs ``rc_text`` and nothing from the host
    (/etc/profile may reset HOME or print its own banner)."""
    rc = tmp_path / "banner.rc"
    rc.write_text(rc_text)
    login = tmp_path / "bash"
    login.write_text('#!/bin/sh\n[ "$1" = "-lic" ] || exit 97\n'
                     f'exec {shlex.quote(shutil.which("bash"))} --noprofile -lic '
                     f'{shlex.quote(". " + shlex.quote(str(rc)) + "; ")}"$2"\n')
    login.chmod(0o700)
    monkeypatch.setenv("SHELL", str(login))


@pytest.mark.parametrize("mode", ["pipe", "log", "pty"])
def test_rc_banner_never_reaches_output_or_dock(tmp_path, monkeypatch, mode):
    from hermes_cli import cli_process_dock
    from agent.i18n import t
    import tools.process_registry as process_registry_module

    if mode == "pty":
        pytest.importorskip("ptyprocess")
    # The unterminated last line must not swallow the command's first line.
    _banner_shell(tmp_path, monkeypatch, f"printf '%s\\n%s' {shlex.quote(BANNER)} 'no-newline-banner'\n")
    reg = ProcessRegistry()
    monkeypatch.setattr(process_registry_module, "process_registry", reg)
    kwargs = {"persist_on_release": True} if mode == "log" else {"use_pty": True} if mode == "pty" else {}
    try:
        talker = reg.spawn_local("echo first-line; sleep 30", task_id="t-banner", **kwargs)
        quiet = reg.spawn_local("echo; sleep 30", task_id="t-banner", **kwargs)
        assert bool(talker.output_log) == (mode == "log") and (talker._pty is not None) == (mode == "pty")
        wait_until(lambda: "first-line" in talker.output_buffer, what="the command's first line")
        # The quiet job's only output is one newline, written after anything its rc printed.
        wait_until(lambda: "\n" in quiet.output_buffer, what="the quiet job's newline")
        assert talker.output_buffer.replace("\r\n", "\n") == "first-line\n"
        assert quiet.output_buffer.strip() == ""
        rows = {row["id"]: row for row in cli_process_dock.process_rows()}
        assert cli_process_dock.process_activity(rows[talker.id]).endswith(
            t("cli.dock.last_output", detail="first-line"))
        assert cli_process_dock.process_activity(rows[quiet.id]).endswith(t("cli.dock.starting"))
    finally:
        reg.kill_all()


def test_output_is_kept_when_the_shell_never_reaches_the_command(tmp_path, monkeypatch):
    """An rc file that exits never runs the command: what it printed is all the job's output."""
    _banner_shell(tmp_path, monkeypatch, "echo rc-gave-up; exit 5\n")
    reg = ProcessRegistry()
    try:
        s = reg.spawn_local("echo never-runs", task_id="t-banner")
        wait_until(lambda: s.exited, what="job exit")
        assert s.output_buffer.strip() == "rc-gave-up" and s.exit_code == 5
    finally:
        reg.kill_all()


def test_readopted_log_strips_the_banner(tmp_path, monkeypatch):
    """A re-adopted persisted job re-reads its log from the start: the checkpointed marker still gates it."""
    import tools.process_registry as process_registry_module

    assert "start_marker" in process_registry_module._CHECKPOINT_FIELDS
    marker = "__hermes_bg_start_test__"
    log = tmp_path / "proc_readopt.log"
    log.write_text(f"{BANNER}\n{marker}\nreal-output\n")
    (tmp_path / "proc_readopt.exit").write_text("0\n")
    session = ProcessSession(id="proc_readopt", command="x", output_log=str(log), detached=True,
                             start_marker=marker)
    reg = ProcessRegistry()
    monkeypatch.setattr(reg, "_detached_host_fate", lambda *_: "gone")
    reg._log_reader_loop(session)
    assert session.output_buffer == "real-output\n" and session.exit_code == 0
    # An adopted log whose shell never printed the marker keeps everything it wrote.
    log.write_text("rc-gave-up\n")
    (tmp_path / "proc_readopt.exit").write_text("5\n")
    gave_up = ProcessSession(id="proc_readopt2", command="x", output_log=str(log), detached=True,
                             start_marker=marker)
    reg._log_reader_loop(gave_up)
    assert gave_up.output_buffer == "rc-gave-up\n" and gave_up.exit_code == 5


def test_replaced_argv_without_the_marker_is_never_held(monkeypatch):
    """A login argv that prints no marker (Windows, a test's rc-free shell) must not hold the output."""
    import tools.process_registry as process_registry_module
    from tools.environments.local import _find_bash

    monkeypatch.setattr(process_registry_module, "bg_login_argv",
                        lambda command, *_: [_find_bash(), "--noprofile", "--norc", "-c", command])
    reg = ProcessRegistry()
    try:
        s = reg.spawn_local("echo unheld; sleep 30", task_id="t-banner")
        assert s.start_marker == ""
        wait_until(lambda: "unheld" in s.output_buffer, what="output of a running job")
    finally:
        reg.kill_all()


@pytest.mark.parametrize("split", [12, -1], ids=["inside-marker", "before-final-lf"])
def test_overflow_preserves_split_marker_line(split):
    from tools.process_registry_bg_shell import START_HOLD_MAX_CHARS, gate_start_output, release_start_output

    marker = "__hermes_bg_start_overflow__"
    marker_line = f"\r\n{marker}\r\n"
    session = ProcessSession(id="proc_split_overflow", command="x", start_marker=marker)
    flood = "x" * START_HOLD_MAX_CHARS
    # The overflow read ends mid-marker or at the CR/LF seam of its terminator.
    released = gate_start_output(session, flood + "banner-tail" + marker_line[:split])
    assert released and released.startswith("x")
    assert "__hermes" not in released
    completed = gate_start_output(session, marker_line[split:] + "command-output\r\n")
    assert completed == "command-output\r\n"
    assert release_start_output(session) == ""
    assert gate_start_output(session, "later-output\n") == "later-output\n"


def test_gate_holds_split_marker_and_bounds_the_hold():
    from tools.process_registry_bg_shell import START_HOLD_MAX_CHARS, gate_start_output, release_start_output

    s = ProcessSession(id="proc_gate", command="x", start_marker="__m__")
    assert gate_start_output(s, "banner\n__") == ""
    assert gate_start_output(s, "m__\r") == ""  # a PTY line end split across reads
    assert gate_start_output(s, "\nout\n") == "out\n"
    assert gate_start_output(s, "__m__\n") == "__m__\n"  # after the marker, nothing is filtered
    flood = ProcessSession(id="proc_flood", command="x", start_marker="__m__")
    released = gate_start_output(flood, "x" * (START_HOLD_MAX_CHARS + 100))
    assert released and len(released) < START_HOLD_MAX_CHARS + 100
    assert gate_start_output(flood, "\n__m__\nafter") == "after"
    plain = ProcessSession(id="proc_plain", command="x")  # no marker: adopted foreground, Windows, old checkpoint
    assert gate_start_output(plain, "anything") == "anything" and release_start_output(plain) == ""
