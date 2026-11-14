"""Fail-closed boundaries missed by the original relaunch/quiet-control suite."""
import json
import sys
import types

import pytest

from gateway import quiet_control
from hermes_cli import code_attestation, kanban_quiet
from tests.gateway import test_quiet_control as helpers


env = helpers.env


@pytest.mark.parametrize("location", ["outside", "site-packages", "dist-packages", "stdlib"])
def test_relaunch_prefix_module_outside_stdlib_refuses(tmp_path, monkeypatch, location):
    stdlib = tmp_path / "stdlib"
    monkeypatch.setattr(code_attestation.sysconfig, "get_path", lambda key: str(stdlib))
    for name in code_attestation.RELAUNCH_PREFIX_MODULES:
        directory = stdlib
        if name == "runpy":
            directory = tmp_path / "outside" if location == "outside" else stdlib
            if location in {"site-packages", "dist-packages"}:
                directory /= location
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / (name.replace(".", "_") + ".py")
        source.write_text("# readable Python source\n", encoding="utf-8")
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(__file__=str(source)))
    if location == "stdlib":
        assert {row["module"] for row in code_attestation._relaunch_prefix_modules()} == set(
            code_attestation.RELAUNCH_PREFIX_MODULES)
    else:
        with pytest.raises(code_attestation._Refuse,
                           match="^unsupported-route:relaunch-prefix-module:runpy$"):
            code_attestation._relaunch_prefix_modules()


@pytest.mark.parametrize("verb", ["quiet-observe-start", "quiet-observe-check"])
def test_unreadable_code_ledger_refuses_start_and_check(env, verb):
    helpers.attest(env)

    class Unreadable(helpers.CleanLedger):
        def unknowns(self):
            raise OSError("ledger unavailable")

    env.monkeypatch.setattr(env.runner, "_quiet_code", Unreadable())
    params = helpers.start_params(env) if verb.endswith("start") else {"schema": 1}
    result = helpers.ok(env, verb, params)
    assert "unknown:code:unreadable" in result["reasons"]
    assert result.get("admitted", result.get("valid")) is False
    assert "observer_token" not in result


@pytest.mark.parametrize("verb", ["quiet-observe-start", "quiet-observe-check"])
def test_check_inventories_unlatched_code_unknowns(env, verb):
    helpers.attest(env)

    class InventoryOnly(helpers.CleanLedger):
        def unknowns(self):
            return ("late-module",)

    ledger = InventoryOnly()
    assert ledger.latched_unknowns() == ()
    env.monkeypatch.setattr(env.runner, "_quiet_code", ledger)
    params = helpers.start_params(env) if verb.endswith("start") else {"schema": 1}
    result = helpers.ok(env, verb, params)
    assert "unknown:code:late-module" in result["reasons"]
    assert result.get("admitted", result.get("valid")) is False
    assert "observer_token" not in result


@pytest.mark.parametrize("admitted", ["yes", 1, None])
def test_check_malformed_admitted_reads_gateway_unavailable(tmp_path, capsys, admitted):
    def query(*args, **kwargs):
        return {"schema": 1, "phase": "check", "admitted": admitted, "reasons": []}

    assert kanban_quiet.check(tmp_path, query=query) == kanban_quiet.EXIT_CHECK_REFUSED
    doc = json.loads(capsys.readouterr().out)
    assert doc["admitted"] is False
    assert doc["reasons"] == ["gateway_unavailable"]


def test_bind_preserves_bootstrap_code_ledger_after_installed_replacement(env):
    captured = helpers.CleanLedger(("bootstrap-unknown",))
    env.monkeypatch.setattr(code_attestation, "_installed", captured)
    env.monkeypatch.setitem(quiet_control._boot, "captured", False)
    quiet_control.capture_bootstrap_registry()
    env.monkeypatch.setattr(code_attestation, "_installed", helpers.CleanLedger())
    quiet_control.bind_registry(env.runner)
    helpers.attest(env)
    assert env.runner._quiet_code is captured
    result = helpers.ok(env, "quiet-observe-check", {"schema": 1})
    assert result["admitted"] is False
    assert "unknown:code:bootstrap-unknown" in result["reasons"]
