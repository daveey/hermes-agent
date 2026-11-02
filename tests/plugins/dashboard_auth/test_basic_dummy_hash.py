"""The unknown-username dummy hash: free at import, same scrypt cost at verify. [daveey]

Building it with ``hash_password`` ran a 16 MiB scrypt on every import of the plugin; fresh-home plugin
discovery re-imports it, and in tests/tui_gateway/test_tui_gateway_server.py the repeated 16 MiB blocks
fragmented the allocator by ~125 MB of peak RSS.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest

import plugins.dashboard_auth.basic as basic_plugin
from hermes_cli.dashboard_auth import InvalidCredentialsError


def _counting_scrypt(monkeypatch):
    calls = []
    real = hashlib.scrypt

    def _scrypt(password, **kwargs):
        calls.append(kwargs)
        return real(password, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", _scrypt)
    return calls


def test_importing_the_plugin_runs_no_scrypt(monkeypatch):
    calls = _counting_scrypt(monkeypatch)
    path = Path(basic_plugin.__file__)
    spec = importlib.util.spec_from_file_location("_basic_auth_import_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert calls == []
    assert module._DUMMY_HASH == basic_plugin._DUMMY_HASH


def test_dummy_hash_carries_the_live_cost_parameters():
    scheme, n, r, p, salt_b64, dk_b64 = basic_plugin._DUMMY_HASH.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (basic_plugin._SCRYPT_N, basic_plugin._SCRYPT_R, basic_plugin._SCRYPT_P)
    assert len(base64.b64decode(salt_b64)) == basic_plugin._SCRYPT_SALT_BYTES
    assert len(base64.b64decode(dk_b64)) == basic_plugin._SCRYPT_DKLEN


@pytest.mark.parametrize("password", ["dummy-password-for-constant-time-verify", "hunter2", ""])
def test_unknown_username_still_pays_one_full_cost_scrypt_and_is_refused(monkeypatch, password):
    provider = basic_plugin.BasicAuthProvider(
        username="admin", password_hash=basic_plugin.hash_password("right"), secret=b"s" * 32)
    calls = _counting_scrypt(monkeypatch)
    with pytest.raises(InvalidCredentialsError):
        provider.complete_password_login(username="someone-else", password=password)
    assert len(calls) == 1
    assert (calls[0]["n"], calls[0]["r"], calls[0]["p"]) == (
        basic_plugin._SCRYPT_N, basic_plugin._SCRYPT_R, basic_plugin._SCRYPT_P)
    assert basic_plugin._verify_password(password, basic_plugin._DUMMY_HASH) is False
