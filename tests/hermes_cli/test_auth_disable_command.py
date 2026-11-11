"""`hermes auth disable|enable` round-trip and the `auth list` tag."""
import argparse
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    rows = [{"id": f"row{i}", "label": f"acct{i}", "auth_type": "api_key", "priority": i,
             "source": "manual", "access_token": f"sk-or-fixture-{i}"} for i in range(2)]
    (hermes_home / "auth.json").write_text(json.dumps(
        {"version": 1, "providers": {}, "credential_pool": {"openrouter": rows}}))
    from agent import credential_pool_disabled as cpd
    monkeypatch.setattr(cpd, "_root_path", lambda: None)
    cpd._cache.clear()
    return hermes_home


def _run(verb, target, *, parse=True):
    from hermes_cli import auth_commands
    args = SimpleNamespace(auth_action=verb, provider="openrouter", target=target)
    if parse:  # through the real `hermes auth` subcommand parser
        from hermes_cli.subcommands.auth import build_auth_parser
        parser = argparse.ArgumentParser()
        build_auth_parser(parser.add_subparsers(dest="command"), cmd_auth=lambda a: None)
        args = parser.parse_args(["auth", verb, "openrouter", target])
        assert args.auth_action == verb and args.target == target
    auth_commands.auth_command(args)


def _list(capsys):
    from hermes_cli.auth_commands import auth_list_command
    capsys.readouterr()
    auth_list_command(SimpleNamespace(provider="openrouter"))
    return {line.split()[1]: line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("#")}


def test_disable_enable_round_trip(home, capsys):
    from agent import credential_pool_disabled as cpd
    from agent.credential_pool import load_pool

    _run("disable", "acct0")
    assert "Disabled openrouter credential #1 (acct0)" in capsys.readouterr().out
    cpd._cache.clear()
    rows = _list(capsys)
    assert "[disabled]" in rows["acct0"] and "[disabled]" not in rows["acct1"]
    assert load_pool("openrouter").select().id == "row1"
    stored = json.loads((home / "auth.json").read_text())
    assert stored["disabled_credentials"] == {"openrouter": [{"id": "row0", "label": "acct0"}]}

    _run("disable", "row0")
    assert "(already disabled)" in capsys.readouterr().out

    _run("enable", "1")
    assert "Enabled openrouter credential #1 (acct0)" in capsys.readouterr().out
    cpd._cache.clear()
    assert "[disabled]" not in _list(capsys)["acct0"]
    assert "disabled_credentials" not in json.loads((home / "auth.json").read_text())


def test_unknown_target_exits(home):
    with pytest.raises(SystemExit, match="No credential matching"):
        _run("disable", "nobody", parse=False)


def test_enable_warns_when_root_disables(home, monkeypatch, tmp_path, capsys):
    from agent import credential_pool_disabled as cpd
    root = tmp_path / "root.json"
    root.write_text(json.dumps({"providers": {}, "disabled_credentials": {"openrouter": [{"id": "row0"}]}}))
    monkeypatch.setattr(cpd, "_root_path", lambda: root)
    _run("enable", "acct0", parse=False)
    out = capsys.readouterr().out
    assert "(already enabled)" in out and "global Hermes home also disables" in out
