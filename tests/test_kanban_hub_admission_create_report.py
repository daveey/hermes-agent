"""A card hub admission denied reports its final pool placement, not the pre-judge marker. [daveey]

Self-heal t_01a1cfae6c9413c8 (defect agent-aaab0fddf3): ``hermes kanban create --json`` on the hub
printed ``placement: {where: local, why: "home gateway marker: mentions '~/.hermes/l'"}`` for a card
the judge had denied and queued for ``pool:worker`` + ``capability:hub-write``. Agents read
``where: local`` / ``runs_where: here (hub)`` as a hub pin and re-placed the card by hand.
Fake judge only (never a live model).
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import argparse
import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import hub_judge_support
from tests.hermes_cli import test_kanban_portable_reason_audit as audit_support
from tests.hermes_cli import test_kanban_worker_policy_db as fixtures
from tools import kanban_tools as kt

conn = fixtures.conn
kanban_home = audit_support.kanban_home
hub_judge = hub_judge_support.hub_judge
MARKED = "Read ~/.hermes/logs/agent.log on the hub and summarise the errors"
QUEUE_CFG = {"worker_pools": {"hub": []}, "worker_queue": {"hub": "pool:worker"}}


def _cli_create(*argv):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    return cli._cmd_create(parser.parse_args(["kanban", "create", *argv]))


@pytest.fixture
def hub(monkeypatch, hub_judge):
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(placement, "_machine_picks_up", lambda labels: "pool:worker" in labels)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    return hub_judge


def _assert_pool(report, *, queued):
    assert report["where"] == "pool", report
    assert "home gateway marker" not in report["why"]
    assert "fake judge denies" in report["why"]
    assert report["hub_admission"]["admit"] is False
    if queued:
        assert {"pool:worker", "capability:hub-write"} <= set(report["runs_on"])
    else:
        assert report["placement_policy"] == "worker-first-v1" and not report.get("runs_on")


@pytest.mark.parametrize("mode,queued", [("primary", True), ("off", False)])
def test_cli_json_reports_the_denied_card_in_the_pool(conn, hub, monkeypatch, capsys, mode, queued):
    monkeypatch.setattr(placement, "config", lambda: QUEUE_CFG)
    monkeypatch.setattr(sync, "board_mode", lambda _conn: mode)
    hub.admit_next = False
    assert _cli_create("t", "--body", MARKED, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    _assert_pool(out["placement"], queued=queued)
    assert "not on the hub" in out["placement"]["note"]
    with kbc.connect_closing() as db:  # the report matches what was persisted
        raw = db.execute("SELECT runs_on, placement_policy FROM tasks WHERE id = ?", (out["id"],)).fetchone()
    assert (json.loads(raw[0]) if raw[0] else None) == out["placement"].get("runs_on")
    assert raw[1] == out["placement"].get("placement_policy")


def test_cli_human_output_names_the_pool(conn, hub, monkeypatch, capsys):
    monkeypatch.setattr(placement, "config", lambda: QUEUE_CFG)
    monkeypatch.setattr(sync, "board_mode", lambda _conn: "primary")
    hub.admit_next = False
    assert _cli_create("t", "--body", MARKED) == 0
    text = capsys.readouterr().out
    assert "home gateway marker" not in text
    assert "hub admission denied" in text and "pool:worker" in text


def test_cli_admitted_card_still_reports_local(conn, hub, monkeypatch, capsys):
    """Control: an admitted card stays on the hub and says so."""
    monkeypatch.setattr(placement, "config", lambda: QUEUE_CFG)
    monkeypatch.setattr(sync, "board_mode", lambda _conn: "primary")
    hub.admit_next = True
    assert _cli_create("t", "--body", MARKED, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["placement"]["where"] == "local"
    assert "home gateway marker" in out["placement"]["why"]
    assert not out["placement"].get("runs_on")
    with kbc.connect_closing() as db:
        task = kb.get_task(db, out["id"])
    assert task is not None and task.placement_policy is None


@pytest.fixture
def tool_hub(kanban_home, hub, monkeypatch):
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(kanban_home))
    from hermes_cli import kanban_sync_client as ksc
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  sync:\n    enabled: true\n  placement:\n    worker_pools:\n      hub: []\n"
        "    worker_queue:\n      hub: pool:worker\n")
    kb.init_db()
    with kbc.connect(kanban_home / "kanban.db") as db:
        ksc.ensure_identity(db, "default", "hub")
        sync.set_mode(db, "primary")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    return hub


def test_tool_result_reports_the_denied_card_in_the_pool(tool_hub):
    tool_hub.admit_next = False
    result = json.loads(kt._handle_create({"title": "t", "body": MARKED, "requires": []}))
    assert result.get("ok"), result
    _assert_pool(result["placement"], queued=True)
    assert "pool" in result["runs_where"] and "here" not in result["runs_where"]
    assert "not on the hub" in result["note"]


def test_tool_unsynced_board_reports_the_worker_pool(kanban_home, hub, monkeypatch):
    """A board with no Board sync has no runs_on column: the denied card is worker-first."""
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(kanban_home))
    (kanban_home / "config.yaml").write_text("kanban:\n  placement:\n    worker_pools:\n      hub: []\n")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    hub.admit_next = False
    result = json.loads(kt._handle_create({"title": "t", "body": MARKED, "requires": []}))
    assert result.get("ok"), result
    _assert_pool(result["placement"], queued=False)
    assert result["runs_where"].startswith("worker pool")


def test_tool_admitted_card_still_runs_here(tool_hub):
    tool_hub.admit_next = True
    result = json.loads(kt._handle_create({"title": "t", "body": MARKED, "requires": []}))
    assert result.get("ok"), result
    assert result["placement"]["where"] == "local"
    assert result["runs_where"] == "here (hub)"
