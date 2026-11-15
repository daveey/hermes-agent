"""Exercise declarations through both real creation front doors."""
import argparse
import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_selfwork as policy
from tests.hermes_cli import test_kanban_selfwork as support
from tools import kanban_tools as kt

board = support.board


@pytest.fixture
def local(board, monkeypatch):
    monkeypatch.setattr(placement, "config", dict)
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "test-worker")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_ORIGIN_SESSION",
                "HERMES_SESSION_KEY", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    return board


def create(entry, value):
    if entry == "tool":
        args = {"title": "Scoped implementation", "requires": [], "placement": "local"}
        if value is not None:
            args["self_work"] = value
        return json.loads(kt._handle_create(args))
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    argv = ["kanban", "create", "Scoped implementation", "--json"]
    if value is not None:
        argv += ["--self-work", json.dumps(value)]
    return cli._cmd_create(parser.parse_args(argv))


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("value", [None])
def test_creation_persists_declaration(local, entry, value):
    result = create(entry, value)
    assert result == 0 if entry == "cli" else result.get("ok"), result
    tasks = kb.list_tasks(local)
    assert len(tasks) == 1
    assert policy.declared(local, tasks[0].id) == value


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("value", [{}, [], support.declaration(version=2), support.declaration("check")])
def test_invalid_declaration_creates_nothing(local, entry, value):
    result = create(entry, value)
    assert result == 2 if entry == "cli" else not result.get("ok"), result
    assert not kb.list_tasks(local)


def test_courier_preserves_object_type():
    value = support.declaration('ci-flake')
    assert placement.carried_options({"self_work": value}) == {"self_work": value}
    assert "self_work" in placement.LEGACY_UNCARRIED


@pytest.mark.parametrize("entry", ["tool", "cli"])
def test_lineage_refusal_is_reported_without_partial_creation(local, entry):
    result = create(entry, support.declaration("check", origin="t_missing"))
    assert result == 2 if entry == "cli" else not result.get("ok"), result
    assert not kb.list_tasks(local)


@pytest.mark.parametrize("value", [{}, [], False, "{}"])
def test_courier_refuses_malformed_declarations(value):
    with pytest.raises(ValueError):
        placement.carried_options({"self_work": value})
