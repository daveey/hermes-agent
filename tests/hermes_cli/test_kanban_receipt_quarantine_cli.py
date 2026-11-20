"""``hermes kanban sync quarantine`` and the ``sync status`` line, through ``cmd_sync`` (review r3, item 5)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_cli as cli
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


@pytest.fixture
def board(synced_conn, kanban_home, monkeypatch):
    """The fixture-board file plays the replica (shadow); a second synced file plays this gateway's live board."""
    case = support.conflict(synced_conn)
    shadow = Path(synced_conn.execute("PRAGMA database_list").fetchone()[2])
    live = kanban_home / "live.db"
    with kbc.connect_closing(db_path=live) as conn:
        sync.set_identity(conn, "fixture", "worker")
    monkeypatch.setattr(cli, "_path", lambda slug: live)
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: shadow)
    monkeypatch.setattr(ksc, "load_identity", lambda: SimpleNamespace(gateway="worker", url="https://board.test"))
    monkeypatch.setattr(ksc, "Hub", lambda ident: case.hub)
    return SimpleNamespace(case=case, shadow=synced_conn, live=live)


def _run(**kw) -> int:
    args = argparse.Namespace(sync_action="quarantine", board="fixture", json=kw.pop("json", False),
                              release=kw.pop("release", None), reason=kw.pop("reason", None))
    return cli.cmd_sync(args)


def test_listing_names_each_receipt_and_the_tasks_it_holds(board, capsys):
    assert _run(json=True) == 0
    out = json.loads(capsys.readouterr().out)
    [entry] = out["quarantined"]
    assert entry["receipt"] == board.case.uid and entry["kind"] == "identity_changed"
    assert board.case.target in entry["held_tasks"] and board.case.child in entry["held_tasks"]


def test_release_requires_a_reason(board, capsys):
    assert _run(release=board.case.uid) == 2
    assert "--reason" in capsys.readouterr().out
    assert _run(release=board.case.uid, reason="   ") == 2
    assert quarantine.held(board.shadow, board.case.target)


def test_release_of_an_unknown_receipt_exits_1(board, capsys):
    assert _run(release="no-such-receipt", reason="typo") == 1
    assert "no quarantined receipt no-such-receipt" in capsys.readouterr().out


def test_release_refused_while_board_evidence_is_not_live_exits_2(board, capsys):
    board.case.hub.events[board.case.uid]["state"] = "deleted"
    assert _run(release=board.case.uid, reason="ruled benign") == 2
    assert "not released" in capsys.readouterr().out
    assert quarantine.held(board.shadow, board.case.target)


def test_release_accepts_replays_and_lifts_the_hold(board, capsys):
    assert _run(release=board.case.uid, reason="ruled benign") == 0
    out = capsys.readouterr().out
    assert f"released {board.case.uid}" in out and "accepted" in out
    assert not quarantine.held(board.shadow, board.case.target)
    assert support.count(board.shadow, "containment_sync_acceptances") == 1
    assert board.shadow.execute("SELECT 1 FROM task_events WHERE uid=?", (board.case.uid,)).fetchone()


def test_release_while_a_round_holds_the_board_exits_2(board, capsys, monkeypatch):
    from hermes_cli import kanban_sync_guard as guard

    def busy(path):
        raise guard.SyncRoundBusy("board sync round is busy")
    monkeypatch.setattr(guard, "board_sync_lock", busy)
    assert _run(release=board.case.uid, reason="ruled benign") == 2
    assert "busy" in capsys.readouterr().out
    assert quarantine.held(board.shadow, board.case.target)


def test_status_line_points_at_the_quarantine_command(board, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_path", lambda slug: Path(board.shadow.execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: Path("/nonexistent/shadow.db"))
    args = argparse.Namespace(sync_action="status", board="fixture", json=True)
    assert cli.cmd_sync(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["quarantined"] == "1 receipt(s) held; run `hermes kanban --board fixture sync quarantine`"
