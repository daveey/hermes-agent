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
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    rows = [{"id": f"row{i}", "label": f"acct{i}", "auth_type": "api_key", "priority": i,
             "source": "manual", "access_token": f"sk-or-fixture-{i}"} for i in range(2)]
    (hermes_home / "auth.json").write_text(json.dumps(
        {"version": 1, "providers": {}, "credential_pool": {"anthropic": rows}}))
    from agent import credential_pool_disabled as cpd
    monkeypatch.setattr(cpd, "_root_path", lambda: None)
    cpd._cache.clear()
    return hermes_home


def _run(verb, target, *, parse=True):
    from hermes_cli import auth_commands
    args = SimpleNamespace(auth_action=verb, provider="anthropic", target=target)
    if parse:  # through the real `hermes auth` subcommand parser
        from hermes_cli.subcommands.auth import build_auth_parser
        parser = argparse.ArgumentParser()
        build_auth_parser(parser.add_subparsers(dest="command"), cmd_auth=lambda a: None)
        args = parser.parse_args(["auth", verb, "anthropic", target])
        assert args.auth_action == verb and args.target == target
    auth_commands.auth_command(args)


def _list(capsys):
    from hermes_cli.auth_commands import auth_list_command
    capsys.readouterr()
    auth_list_command(SimpleNamespace(provider="anthropic"))
    return {line.split()[1]: line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("#")}


def test_disable_enable_round_trip(home, capsys):
    from agent import credential_pool_disabled as cpd
    from agent.credential_pool import load_pool

    _run("disable", "acct0")
    assert "Disabled anthropic credential #1 (acct0)" in capsys.readouterr().out
    cpd._cache.clear()
    rows = _list(capsys)
    assert "[disabled]" in rows["acct0"] and "[disabled]" not in rows["acct1"]
    assert load_pool("anthropic").select().id == "row1"
    stored = json.loads((home / "auth.json").read_text())
    (record,) = stored["disabled_credentials"]["anthropic"]
    assert {k: record[k] for k in ("id", "label")} == {"id": "row0", "label": "acct0"}
    assert record["fp"].startswith("sha256:") and "***" not in json.dumps(stored["disabled_credentials"])

    _run("disable", "row0")
    assert "(already disabled)" in capsys.readouterr().out

    _run("enable", "1")
    assert "Enabled anthropic credential #1 (acct0)" in capsys.readouterr().out
    cpd._cache.clear()
    assert "[disabled]" not in _list(capsys)["acct0"]
    assert "disabled_credentials" not in json.loads((home / "auth.json").read_text())


def test_unknown_target_exits(home):
    with pytest.raises(SystemExit, match="No credential matching"):
        _run("disable", "nobody", parse=False)


def test_enable_warns_when_root_disables(home, monkeypatch, tmp_path, capsys):
    from agent import credential_pool_disabled as cpd
    root = tmp_path / "root.json"
    root.write_text(json.dumps({"providers": {}, "disabled_credentials": {"anthropic": [{"id": "row0"}]}}))
    monkeypatch.setattr(cpd, "_root_path", lambda: root)
    _run("enable", "acct0", parse=False)
    out = capsys.readouterr().out
    assert "(already enabled)" in out and "global Hermes home also disables" in out


@pytest.mark.parametrize("location", ["active", "root"])
@pytest.mark.parametrize("local", [False, True])
def test_enable_reports_remaining_relay_policy(home, monkeypatch, tmp_path, capsys, location, local):
    from agent import credential_pool_disabled as cpd
    from agent.credential_pool import load_pool
    from hermes_cli import auth

    store = auth._load_auth_store()
    store["credential_pool"]["anthropic"][0]["source"] = "manual:hub_minted"
    auth._save_auth_store(store)
    if local:
        _run("disable", "acct0")
    record = {"label": "acct0", "source": "hub-oauth-relay", "scope": "manual:hub_minted"}
    if location == "active":
        store = auth._load_auth_store()
        store.setdefault("disabled_credentials", {}).setdefault("anthropic", []).append(record)
        auth._save_auth_store(store)
    else:
        root = tmp_path / "root.json"
        root.write_text(json.dumps({"disabled_credentials": {"anthropic": [record]}}))
        monkeypatch.setattr(cpd, "_root_path", lambda: root)
    capsys.readouterr()
    _run("enable", "acct0")
    out = capsys.readouterr().out
    assert "Still disabled" in out and "hub-oauth-relay" in out
    assert "already enabled" not in out
    assert cpd.is_credential_disabled("anthropic", load_pool("anthropic").entries()[0])


@pytest.mark.parametrize("root_data", [None, "not-json", '{"disabled_credentials": {}}'])
def test_relay_report_ignores_unreadable_or_empty_root(home, monkeypatch, tmp_path, root_data):
    from agent import credential_pool_disabled as cpd
    from agent.credential_pool import load_pool
    root = tmp_path / "root.json"
    if root_data is not None:
        root.write_text(root_data)
    monkeypatch.setattr(cpd, "_root_path", lambda: root)
    assert not cpd.disabled_by_relay("anthropic", load_pool("anthropic").entries()[0])


@pytest.mark.parametrize("record", [
    {"label": "acct0"},
    {"label": "other", "source": "hub-oauth-relay"},
    {"label": "acct0", "source": "hub-oauth-relay", "scope": "manual:hub_minted"},
])
def test_relay_report_requires_owned_matching_record(home, record):
    from agent import credential_pool_disabled as cpd
    from agent.credential_pool import load_pool
    from hermes_cli import auth
    store = auth._load_auth_store()
    store["disabled_credentials"] = {"anthropic": [record]}
    auth._save_auth_store(store)
    assert not cpd.disabled_by_relay("anthropic", load_pool("anthropic").entries()[0])


def test_remove_forgets_the_disable_so_a_re_added_row_is_not_born_disabled(home):
    from hermes_cli.auth_commands import auth_remove_command
    _run("disable", "acct0")
    auth_remove_command(SimpleNamespace(provider="anthropic", target="acct0"))
    assert "disabled_credentials" not in json.loads((home / "auth.json").read_text())
