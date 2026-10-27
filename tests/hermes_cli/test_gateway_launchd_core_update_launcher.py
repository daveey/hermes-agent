"""[daveey] The launchd gateway plist runs stderr_timestamp under the installed core-update launcher.

See hermes_cli.gateway_launchd.gateway_launcher_prefix and the state repo's docs/core-update-safety.md.
"""
import plistlib
import shlex
from pathlib import Path

import hermes_cli.gateway as gateway_cli
from gateway import status
from tests.hermes_cli.test_gateway_service import _osascript_exec_argv


class TestCoreUpdateLauncherPlist:
    def _install_launcher(self, tmp_path, mode=0o755):
        launcher = tmp_path / ".hermes" / "bin" / "hermes-gateway-launcher"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/usr/bin/env python3\n")
        launcher.chmod(mode)
        return launcher

    def _signed_plist_exec_argv(self, tmp_path, monkeypatch):
        profile_dir = tmp_path / ".hermes"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        monkeypatch.setattr(gateway_cli, "get_python_path", lambda: "/usr/bin/python3")
        signed = tmp_path / "Applications" / "Hermes Python.app" / "Contents" / "MacOS" / "python3.14"
        from hermes_cli import gateway_launchd
        monkeypatch.setattr(gateway_launchd, "launchd_responsible_python", lambda: signed)
        program_args = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))["ProgramArguments"]
        assert program_args[:3] == [str(signed), "-I", "-c"]
        exec_, *argv = shlex.split(program_args[4])
        assert exec_ == "exec"
        return profile_dir, argv[:-4]

    def test_launchd_plist_runs_stderr_wrapper_under_installed_core_update_launcher(self, tmp_path, monkeypatch):
        """[daveey] With the core-update launcher installed, it wraps stderr_timestamp INSIDE the shim's
        ``sh -c exec`` string (the shim forwards no SIGUSR1 and drops the lock fd), so the template keeps
        the bootstrap instead of reverting it on the next refresh."""
        launcher = self._install_launcher(tmp_path)
        profile_dir, argv = self._signed_plist_exec_argv(tmp_path, monkeypatch)

        assert argv[:4] == [str(launcher.resolve()), "--home", str(profile_dir.resolve()), "--"]
        assert argv[4] == "/usr/bin/python3"
        assert any("hermes_cli.stderr_timestamp" in part for part in argv)
        assert argv[-3:] == ["gateway", "run", "--external-supervisor"]
        # The launcher's own ps line carries the gateway argv as data: never the gateway itself.
        assert status.looks_like_gateway_command_line(shlex.join(argv)) is False
        assert status.looks_like_gateway_command_line(shlex.join(["/usr/bin/python3", *argv])) is False

    def test_launchd_osascript_plist_also_runs_under_installed_launcher(self, tmp_path, monkeypatch):
        """[daveey] The upstream osascript form gets the same prefix inside its shell command."""
        launcher = self._install_launcher(tmp_path)
        profile_dir = tmp_path / ".hermes"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        monkeypatch.setattr(gateway_cli, "get_python_path", lambda: "/usr/bin/python3")

        program_args = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))["ProgramArguments"]
        argv = _osascript_exec_argv(program_args)

        assert argv[:4] == [str(launcher.resolve()), "--home", str(profile_dir.resolve()), "--"]
        assert argv[4] == "/usr/bin/python3"

    def test_launchd_profile_plist_uses_installation_root_launcher(self, tmp_path, monkeypatch):
        """[daveey] A profile's gateway runs under the root's launcher, with its own home for the lock."""
        launcher = self._install_launcher(tmp_path)
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        monkeypatch.setattr(gateway_cli, "get_python_path", lambda: "/usr/bin/python3")

        program_args = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))["ProgramArguments"]
        argv = _osascript_exec_argv(program_args)

        assert argv[:4] == [str(launcher.resolve()), "--home", str(profile_dir.resolve()), "--"]

    def test_launchd_plist_without_launcher_keeps_stderr_wrapper_first(self, tmp_path, monkeypatch):
        """[daveey] No launcher installed (every non-bootstrapped host): the command is unchanged."""
        _profile_dir, argv = self._signed_plist_exec_argv(tmp_path, monkeypatch)

        assert argv[0] == "/usr/bin/python3"
        assert "hermes-gateway-launcher" not in " ".join(argv)

    def test_launchd_plist_ignores_non_executable_launcher(self, tmp_path, monkeypatch):
        """[daveey] A half-installed (non-executable) launcher would fail every launchd spawn: skip it."""
        self._install_launcher(tmp_path, mode=0o644)
        _profile_dir, argv = self._signed_plist_exec_argv(tmp_path, monkeypatch)

        assert argv[0] == "/usr/bin/python3"
