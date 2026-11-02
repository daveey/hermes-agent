"""Tests for systemd private-socket preflight."""

from pathlib import Path

import pytest

pytest.importorskip("pwd")
pytest.importorskip("grp")

import hermes_cli.gateway as gateway_cli


class TestUserSystemdPrivateSocketPreflight:
    def test_preflight_accepts_private_socket_without_dbus_bus(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: None)
        monkeypatch.setattr(gateway_cli, "_user_dbus_socket_path", lambda: Path("/tmp/missing-bus"))
        monkeypatch.setattr(gateway_cli, "_user_systemd_private_socket_path", lambda: Path("/tmp/private-socket"))
        monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/tmp/private-socket")

        gateway_cli._preflight_user_systemd(auto_enable_linger=False)

    def test_wait_for_user_dbus_socket_accepts_private_socket(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: calls.append("env"))
        monkeypatch.setattr(gateway_cli, "_user_dbus_socket_path", lambda: Path("/tmp/missing-bus"))
        monkeypatch.setattr(gateway_cli, "_user_systemd_private_socket_path", lambda: Path("/tmp/private-socket"))
        monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/tmp/private-socket")

        assert gateway_cli._wait_for_user_dbus_socket(timeout=0.1) is True
        assert calls == ["env"]
