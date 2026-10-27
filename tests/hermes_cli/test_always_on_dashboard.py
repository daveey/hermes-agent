"""Always-on remote dashboard: supervisor units, persisted credentials, the run wrapper, idle-exit opt-out."""

import argparse
import os
import plistlib
import stat
from pathlib import Path

import pytest

import hermes_cli.always_on_dashboard as ao
import hermes_cli.web_server as ws_mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv(ao.ENV_FLAG, raising=False)
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_launchd_plist_keeps_the_backend_alive_on_a_fixed_port(tmp_path):
    raw = ao.render_launchd_plist(hermes="/opt/h/bin/hermes", port=9147, home=tmp_path, log_path=tmp_path / "l.log")
    plist = plistlib.loads(raw)
    assert plist["Label"] == ao.LAUNCHD_LABEL
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [
        "/opt/h/bin/hermes", "always-on-dashboard", "run", "--port", "9147", "--hermes", "/opt/h/bin/hermes"]
    assert plist["EnvironmentVariables"]["PATH"].startswith("/opt/h/bin:")
    assert plist["ThrottleInterval"] >= 5


def test_systemd_unit_restarts_after_any_exit():
    unit = ao.render_systemd_unit(hermes="/home/u/.local/bin/hermes", port=9147, log_path=Path("/tmp/l.log"))
    lines = unit.splitlines()
    assert "Restart=always" in lines
    assert "ExecStart=/home/u/.local/bin/hermes always-on-dashboard run --port 9147 --hermes /home/u/.local/bin/hermes" in lines
    assert "WantedBy=default.target" in lines
    assert any(line.startswith("RestartSec=") for line in lines)


def test_credentials_persist_across_restarts_with_private_modes(home):
    token, nonce = ao.ensure_credentials()
    assert len(token) == 64 and len(nonce) == 16
    assert ao.ensure_credentials() == (token, nonce)
    assert _mode(ao.state_dir()) == 0o700
    assert _mode(ao.state_dir() / "token") == 0o600


def test_staged_token_passes_the_real_serve_validator(home):
    from hermes_cli.main_dashboard import _read_ssh_session_token_file

    token, nonce = ao.ensure_credentials()
    path = ao.stage_token_file(token, nonce)
    assert _read_ssh_session_token_file(str(path)) == token
    assert not path.exists()  # serve consumes the one-shot copy; the persisted token stays
    assert (ao.state_dir() / "token").read_text() == token


def test_wait_for_update_clear_blocks_until_the_marker_goes(tmp_path):
    marker = tmp_path / ".hermes-update-in-progress"
    marker.write_text("x")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            marker.unlink()

    assert ao.wait_for_update_clear(marker, sleep=sleep) is True
    assert len(sleeps) == 3
    marker.write_text("x")
    clock = iter([0.0, 0.0, 1000.0])
    assert ao.wait_for_update_clear(marker, timeout_s=10, sleep=lambda s: None, now=lambda: next(clock)) is False


def test_run_execs_the_isolated_ssh_backend_and_publishes_its_record(home, monkeypatch):
    captured = {}

    def fake_execve(path, argv, env):
        captured.update(path=path, argv=argv, env=env)

    monkeypatch.setattr(ao.os, "execve", fake_execve)
    ao.cmd_run(argparse.Namespace(port=9147, hermes="/opt/h/bin/hermes"))
    token, nonce = ao.ensure_credentials()
    argv = captured["argv"]
    assert captured["path"] == "/opt/h/bin/hermes"
    assert argv[:7] == ["/opt/h/bin/hermes", "serve", "--isolated", "--host", "127.0.0.1", "--port", "9147"]
    assert argv[argv.index("--ssh-owner-nonce") + 1] == nonce
    token_path = Path(argv[argv.index("--ssh-session-token-file") + 1])
    assert token_path.parent.name == ao.OWNERSHIP_ID and token_path.read_text() == token
    assert captured["env"]["HERMES_DESKTOP"] == "1" and captured["env"][ao.ENV_FLAG] == "1"
    record = ao.read_record()
    assert record["pid"] == os.getpid() and record["port"] == 9147 and record["spawnNonce"] == nonce
    assert record["hermesHome"] == str(home / ".hermes")


def test_run_parser_is_registered_on_the_hermes_cli():
    parser = argparse.ArgumentParser()
    ao.build_always_on_dashboard_parser(parser.add_subparsers())
    args = parser.parse_args(["always-on-dashboard", "run", "--port", "9150"])
    assert args.func is ao.cmd_run and args.port == 9150


class _Server:
    should_exit = False


def test_idle_exit_is_disabled_only_for_the_always_on_backend(monkeypatch):
    import hermes_cli.web_server_idle_exit as idle

    armed = []
    monkeypatch.setattr(idle, "start_idle_watchdog", lambda server, tracker, grace_s: armed.append(grace_s))
    monkeypatch.setattr(ws_mod, "load_config", dict)
    monkeypatch.setattr(ws_mod.app.state, "ssh_isolated_clients", idle.IdleClientTracker(), raising=False)

    monkeypatch.delenv(ao.ENV_FLAG, raising=False)
    assert ws_mod._arm_ssh_isolated_idle_exit(_Server()) is True  # an ordinary Desktop-spawned SSH backend
    assert armed == [idle.DEFAULT_IDLE_GRACE_S]

    monkeypatch.setenv(ao.ENV_FLAG, "1")
    assert ws_mod._arm_ssh_isolated_idle_exit(_Server()) is False  # the supervised always-on backend
    assert armed == [idle.DEFAULT_IDLE_GRACE_S]
    assert ao.ENV_FLAG not in os.environ  # dropped, so agent children never inherit it

    monkeypatch.setattr(ws_mod.app.state, "ssh_isolated_clients", None, raising=False)
    assert ws_mod._arm_ssh_isolated_idle_exit(_Server()) is False  # not SSH-isolated at all
