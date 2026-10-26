"""Acceptance transport setup and ephemeral credential wrappers."""
import json
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_pr_acceptance as gate


def test_missing_gh_is_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    receipt = gate.collect_acceptance("https://github.com/acme/repo/pull/7", None)
    assert receipt["classification"] == "setup"
    assert "gh" in receipt["detail"]


@pytest.mark.platforms("posix")
def test_gh_in_user_bin(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    folder = tmp_path / "bin"
    folder.mkdir()
    gh = folder / "gh"
    gh.write_text(f"#!{sys.executable}\nprint('{{}}')\n")
    gh.chmod(0o755)
    assert gate._api("graphql") == {}


@pytest.mark.platforms("posix")
def test_profile_wrapper_vends_only_to_gh(tmp_path, monkeypatch):
    import os

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\nimport os,json\n"
                  "print(json.dumps({'vended': os.environ.get('GH_TOKEN') == 'synthetic-token'}))\n")
    gh.chmod(0o755)
    wrapper = tmp_path / "broker.py"
    wrapper.write_text("import os,sys\nos.environ['GH_TOKEN']='synthetic-token'\n"
                       "os.execv(sys.argv[1], sys.argv[1:])\n")
    (home / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance": {
        "gh_command_prefix": [sys.executable, str(wrapper)]}}}))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert gate._api("graphql", profile_home=str(home)) == {"vended": True}
    assert "GH_TOKEN" not in os.environ
    # The launch profile's broker opt-in must never be inherited by a routed profile.
    other = home / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("{}")
    assert gate._api("graphql", profile_home=str(other)) == {"vended": False}
    # Auth refusals must remain auth, with no captured stderr in durable receipts.
    gh.write_text(f"#!{sys.executable}\nimport sys\n"
                  "print('HTTP 403 synthetic-secret', file=sys.stderr)\nsys.exit(1)\n")
    receipt = gate.collect_acceptance("https://github.com/acme/repo/pull/7", None)
    assert receipt["classification"] == "auth"
    assert "synthetic-secret" not in json.dumps(receipt)


@pytest.mark.parametrize("prefix", ["shell command", ["relative-broker"], ["/missing/broker"], [1]])
def test_invalid_wrapper_is_setup(tmp_path, monkeypatch, prefix):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance": {
        "gh_command_prefix": prefix}}}))
    with pytest.raises(gate._GateSetupError):
        gate._gh_command_prefix(str(home))


@pytest.mark.parametrize("text", ['kanban: {pr_acceptance: null}', 'kanban: null', 'kanban: ['])
def test_broken_transport_config_is_sanitized_setup(tmp_path, monkeypatch, text):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(text)
    with pytest.raises(gate._GateSetupError):
        gate._gh_command_prefix(str(tmp_path))


def test_wrapper_interpolation_uses_assignee_scope(tmp_path, monkeypatch):
    from agent.secret_scope import set_secret_scope, reset_secret_scope

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    target = tmp_path / "other"
    target.mkdir()
    (target / ".env").write_text("BROKER_SCOPE=target-scope\n")
    (target / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance": {
        "gh_command_prefix": [sys.executable, "${BROKER_SCOPE}"]}}}))
    token = set_secret_scope({"BROKER_SCOPE": "launch-scope"})
    try:
        assert gate._gh_command_prefix(str(target)) == [sys.executable, "target-scope"]
    finally:
        reset_secret_scope(token)
