"""The same gateway deferring ``hermes update`` three times in a row gets a repair owner.

hermes-hub, 2026-11-11: eleven updates in a row were deferred by a gateway that could never take
the idle pause, and the only trace was one log line per attempt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import update_cmd_posix_pause as pause
from hermes_cli import update_pause_deferrals as deferrals

UNIT = "hermes-gateway-askeve-builder"


def test_refused_units_ride_the_refusal(monkeypatch):
    """The streak counts gateways by name: the refusal must carry them, not only prose."""
    from hermes_cli import update_pause_record as record
    monkeypatch.setattr(record, "mark_stop_sent", lambda *a: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *a: None)
    monkeypatch.setattr(pause, "_ask_to_drain", lambda entry: entry.__setitem__("pause_unsupported", True) or False)
    with pytest.raises(pause.PauseRefused) as refused:
        pause._stop_gateways({}, [{"pid": 4242, "home": "/fixture/a", "kind": "systemd", "unit": "old-gw"}])
    assert refused.value.units == ["old-gw"]


def _board(tmp_path, monkeypatch) -> Path:
    from hermes_cli.config import save_config
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    save_config({"platform_toolsets": {"cli": ["terminal", "file"]}, "terminal": {"backend": "local"}})
    return tmp_path / "board.db"


def _cards():
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        return conn.execute("SELECT id, title, body, idempotency_key, status FROM tasks").fetchall()


def test_third_consecutive_deferral_on_one_gateway_files_one_owner_card(tmp_path, monkeypatch, capsys):
    """Two deferrals are ordinary; the third naming the same gateway files a repair card, later ones
    keep that card, and a pause that goes through ends the streak."""
    board = _board(tmp_path, monkeypatch)
    reason = f"{UNIT} does not support idle pause; restart that gateway on the installed code when idle"

    assert deferrals.note_deferral([UNIT], reason) == []
    assert deferrals.note_deferral([UNIT], reason) == []
    assert not board.exists() or _cards() == [], "a card was filed before the third deferral"

    filed = deferrals.note_deferral([UNIT], reason)
    cards = _cards()
    assert len(cards) == 1 and filed == [cards[0][0]]
    assert UNIT in cards[0][1] and "3 times in a row" in cards[0][2] and reason in cards[0][2]
    assert cards[0][3].startswith("update-pause-deferred:")
    assert f"{UNIT} has deferred 3 updates in a row" in capsys.readouterr().out

    assert deferrals.note_deferral([UNIT], reason) == filed, "the fourth deferral did not keep the owner"
    assert len(_cards()) == 1

    deferrals.note_pause_ok()
    assert not deferrals.state_path().exists()
    assert deferrals.note_deferral([UNIT], reason) == [], "the streak survived a pause that went through"


def test_a_deferral_naming_another_gateway_restarts_the_streak(tmp_path, monkeypatch):
    """Only CONSECUTIVE deferrals naming the same gateway count."""
    _board(tmp_path, monkeypatch)
    monkeypatch.setattr(deferrals, "report", lambda unit, entry: pytest.fail(f"card filed for {unit}: {entry}"))
    for names in ([UNIT], [UNIT], ["hermes-gateway"], [UNIT], [UNIT]):
        assert deferrals.note_deferral(names, "is busy or cannot acknowledge an idle pause; retry when idle") == []
    assert json.loads(deferrals.state_path().read_text())["units"][UNIT]["count"] == 2


def test_a_finished_owner_does_not_swallow_a_streak_that_outlived_it(tmp_path, monkeypatch):
    """The owner card was closed but the gateway still defers: the next report files a new owner."""
    _board(tmp_path, monkeypatch)
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        first = deferrals._report(conn, UNIT, 3, "2026-11-11T00:41:04+00:00", "does not support idle pause")
        assert deferrals._report(conn, UNIT, 4, "2026-11-11T00:41:04+00:00", "does not support idle pause") == first
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
        conn.commit()
        second = deferrals._report(conn, UNIT, 5, "2026-11-11T00:41:04+00:00", "does not support idle pause")
    assert second != first and len(_cards()) == 2


def test_a_card_that_cannot_be_filed_never_changes_the_verdict(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(blocker / "board.db"))
    for _ in range(3):
        assert deferrals.note_deferral([UNIT], "does not support idle pause") == []
    assert "Repair owner: NOT FILED" in capsys.readouterr().out
