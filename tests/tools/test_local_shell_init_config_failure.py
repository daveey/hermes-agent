"""Last-known shell startup policy and observable config-read failures (#1107)."""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from tools.environments import local


@pytest.fixture(autouse=True)
def isolated_shell_init_cache(monkeypatch):
    monkeypatch.setattr(local, "_shell_init_config_cache", {}, raising=False)
    monkeypatch.setattr(local, "_shell_init_config_warned", set(), raising=False)


def test_read_error_keeps_previous_shell_init(caplog):
    with patch("hermes_cli.config.load_config", return_value={
        "terminal": {"shell_init_files": ["/custom/init.sh"], "auto_source_bashrc": False},
    }):
        assert local._read_terminal_shell_init_config() == (["/custom/init.sh"], False)
    with patch("hermes_cli.config.load_config", side_effect=OSError("injected config read failure")):
        assert local._read_terminal_shell_init_config() == (["/custom/init.sh"], False)
    assert "OSError: injected config read failure" in caplog.text
    assert "previously resolved shell init is kept" in caplog.text
    assert caplog.records[-1].levelno == logging.WARNING


def test_profile_first_failure_never_reuses_another_profiles_policy(tmp_path, caplog):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(tmp_path / "a")
    try:
        with patch("hermes_cli.config.load_config", return_value={
            "terminal": {"shell_init_files": ["/custom/a.sh"], "auto_source_bashrc": False},
        }):
            assert local._read_terminal_shell_init_config() == (["/custom/a.sh"], False)
        other = set_hermes_home_override(tmp_path / "b")
        try:
            with patch("hermes_cli.config.load_config", side_effect=OSError("profile B failed")):
                assert local._read_terminal_shell_init_config() == ([], True)
            assert "configured shell init was not applied" in caplog.text
        finally:
            reset_hermes_home_override(other)
        caplog.clear()
        with patch("hermes_cli.config.load_config", side_effect=OSError("profile A failed")):
            assert local._read_terminal_shell_init_config() == (["/custom/a.sh"], False)
        assert "previously resolved shell init is kept" in caplog.text
    finally:
        reset_hermes_home_override(token)


def test_first_read_error_warns_about_default_home_profiles(caplog):
    with patch("hermes_cli.config.load_config", side_effect=OSError("injected config read failure")):
        assert local._read_terminal_shell_init_config() == ([], True)
    assert "OSError: injected config read failure" in caplog.text
    assert "configured shell init was not applied" in caplog.text
    assert "default home profiles are sourced instead" in caplog.text
    assert caplog.records[-1].levelno == logging.WARNING


def test_warning_once_per_exception_type_and_message(caplog):
    for error in [OSError("one"), OSError("one"), OSError("two"), ValueError("one")]:
        with patch("hermes_cli.config.load_config", side_effect=error):
            local._read_terminal_shell_init_config()
    warnings = [r for r in caplog.records if r.name == local.__name__]
    assert len(warnings) == 3
    assert all(r.levelno == logging.WARNING for r in warnings)


@pytest.mark.parametrize("files,auto", [(["new.sh", None, 17], False), ([], False), ("bad", True)])
def test_success_refreshes_cache_without_changing_normalization(files, auto, caplog):
    with patch("hermes_cli.config.load_config", return_value={"terminal": {"shell_init_files": ["old.sh"]}}):
        local._read_terminal_shell_init_config()
    expected = ([str(f) for f in files if f] if isinstance(files, list) else [], auto)
    with patch("hermes_cli.config.load_config", return_value={
        "terminal": {"shell_init_files": files, "auto_source_bashrc": auto},
    }):
        result = local._read_terminal_shell_init_config()
        assert result == expected
        result[0].append("caller-mutation.sh")
    with patch("hermes_cli.config.load_config", side_effect=OSError("failed")):
        assert local._read_terminal_shell_init_config() == expected
    assert "previously resolved shell init is kept" in caplog.text


@pytest.fixture
def unavailable_app():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(503)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/requested", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("read_fails", [False, True])
@pytest.mark.parametrize("snapshot_fallback", [False, True])
def test_real_startup_keeps_configured_shell_function(
    tmp_path, monkeypatch, caplog, read_fails, snapshot_fallback, unavailable_app,
):
    """The injected #1107 OSError must not silently replace configured commands."""
    init = tmp_path / "custom.sh"
    init.write_text('/usr/bin/curl() { printf 200; }\nexport -f /usr/bin/curl\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BASH_ENV", raising=False)
    config = {"terminal": {"shell_init_files": [str(init)], "auto_source_bashrc": False}}
    with patch("hermes_cli.config.load_config", return_value=config):
        assert local._resolve_shell_init_files() == [str(init)]
    # Restrict injection to the shell-init read, leaving unrelated config consumers intact.
    read = local._read_terminal_shell_init_config
    def controlled_read():
        with patch("hermes_cli.config.load_config", return_value=config,
                   side_effect=OSError("injected config read failure") if read_fails else None):
            return read()
    monkeypatch.setattr(local, "_read_terminal_shell_init_config", controlled_read)
    if snapshot_fallback:
        # Force bootstrap failure before spawning; then exercise real login fallback.
        monkeypatch.setattr(local.LocalEnvironment, "_snapshot_script_kwargs",
                            lambda self, cwd: dict(quoted_cwd="/", quoted_snap="/dev/null/not-a-file",
                                                   snap_tmp_template="/dev/null/not-a-file", cwd_marker=self._cwd_marker))
        monkeypatch.setattr(local.LocalEnvironment, "_probe_nonlogin_fallback", lambda self, detail: (False, detail))
    url, requests = unavailable_app
    command = f'/usr/bin/curl -q --noproxy "*" --max-time 5 -s -o /dev/null -w "%{{http_code}}" {url}'
    env = local.LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        assert env._snapshot_ready is not snapshot_fallback
        result = env.execute(command)
        assert result["returncode"] == 0
        assert result["output"].strip() == "200"
        assert requests == []
        # Bypassing the function reaches the real application: it really is 503.
        ordinary = env.execute("command " + command)
        assert ordinary["returncode"] == 0
        assert ordinary["output"].strip() == "503"
        assert requests == ["/requested"]
    finally:
        env.cleanup()
    if read_fails:
        assert "OSError: injected config read failure" in caplog.text
        assert "previously resolved shell init is kept" in caplog.text
