"""Write-denial case parity, independent of the host filesystem's casing."""

import pytest

from agent import file_safety as safety


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "user"
    root = home / ".hermes"
    profile = root / "profiles" / "work"
    monkeypatch.setattr(safety, "_guard_homes", lambda path="": {str(home)})
    monkeypatch.setattr(safety, "_hermes_home_path", lambda: profile)
    monkeypatch.setattr(safety, "_hermes_root_path", lambda: root)
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    return home, root, profile


@pytest.mark.parametrize("relative", [
    ".ENV", "MCP-TOKENS/x", "SESSIONS/history.json", "STATE.DB",
    "VAULT/x", "AUTH/GOOGLE_OAUTH.JSON",
])
@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("entry", [False, True])
def test_hermes_case_denials(homes, relative, index, entry):
    assert safety.get_write_denied_error(str(homes[index] / relative), entry=entry)


@pytest.mark.parametrize("relative", [
    ".SSH/AUTHORIZED_KEYS", ".AWS/credentials", ".CONFIG/GH/hosts.yml", ".NETRC",
])
def test_home_case_denials(homes, relative):
    assert safety.get_write_denied_error(str(homes[0] / relative))


@pytest.mark.parametrize("relative", [".SSH/CONFIG", ".Ssh/Config", ".ssh/config"])
def test_case_approval_still_prompts(homes, relative):
    path = str(homes[0] / relative)
    assert safety.get_write_denied_error(path) is None
    assert safety.is_write_approval_required(path)


@pytest.mark.parametrize("relative", [
    "AUTH.JSON", "auth.json", "MCP-TOKENS-extra/x", "SESSIONS-extra/x", ".ENV.example",
])
def test_explicitly_allowed_control_files_and_siblings(homes, relative):
    # Upstream #45947 intentionally removed auth.json from the write deny-list.
    path = str(homes[1] / relative)
    assert safety.get_write_denied_error(path) is None
    assert not safety.is_write_approval_required(path)


def test_prefix_sibling_and_approval_sibling_not_confused(homes):
    assert safety.get_write_denied_error(str(homes[0] / ".SSH-extra/key")) is None
    assert safety.get_write_denied_error(str(homes[0] / ".SSH/CONFIG-extra"))
    assert not safety.is_write_approval_required(str(homes[0] / ".SSH/CONFIG-extra"))
