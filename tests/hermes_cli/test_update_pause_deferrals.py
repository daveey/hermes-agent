"""The same gateway deferring ``hermes update`` three times in a row, over a streak that has
outlived a drain cool-off, gets a repair owner.

hermes-hub, 2026-11-11: eleven updates in a row were deferred by a gateway that could never take
the idle pause, and the only trace was one log line per attempt. hermes-hub, 2026-11-12: three
instant-drainer retries in 41 s against a busy gateway filed a card; attempts alone are no wall.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_a_supervisor_that_cannot_be_disarmed_names_its_gateway(monkeypatch):
    """A drained gateway whose unit cannot be stopped defers the update too: it must be counted."""
    monkeypatch.setattr(pause, "_manage_cmd", lambda scope: None)
    with pytest.raises(pause.PauseRefused) as refused:
        pause._stop_unit({"unit": "old-gw", "scope": "system", "pid": 4242})
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


T0 = datetime(2026, 11, 12, 15, 9, 37, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    """Patched ``_now``: set ``clock.at = <seconds after T0>`` before each deferral. No sleeps."""
    state = SimpleNamespace(at=0)
    monkeypatch.setattr(deferrals, "_now", lambda: T0 + timedelta(seconds=state.at))
    return state


@pytest.fixture
def reported(monkeypatch):
    report = Mock(return_value="t_fixture_owner")
    monkeypatch.setattr(deferrals, "report", report)
    return report


def _streak():
    return json.loads(deferrals.state_path().read_text())["units"][UNIT]


def test_third_consecutive_deferral_on_one_gateway_files_one_owner_card(tmp_path, monkeypatch, capsys, clock):
    """Two deferrals are ordinary; the third naming the same gateway, once the streak has outlived
    a drain cool-off, files a repair card, later ones keep that card, and a pause that goes through
    ends the streak."""
    board = _board(tmp_path, monkeypatch)
    reason = f"{UNIT} does not support idle pause; restart that gateway on the installed code when idle"

    assert deferrals.note_deferral([UNIT], reason) == []
    clock.at = 600
    assert deferrals.note_deferral([UNIT], reason) == []
    assert not board.exists() or _cards() == [], "a card was filed before the third deferral"

    clock.at = deferrals.MIN_STREAK_S
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


def test_a_card_that_cannot_be_filed_never_changes_the_verdict(tmp_path, monkeypatch, capsys, clock):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(blocker / "board.db"))
    for at in (0, 600, deferrals.MIN_STREAK_S):
        clock.at = at
        assert deferrals.note_deferral([UNIT], "does not support idle pause") == []
    assert "Repair owner: NOT FILED" in capsys.readouterr().out


# --- The streak floor: attempts alone are not a wall (hermes-hub 2026-11-12) -----------------
#
# The instant hub drainer retried a refused apply with a doubling delay: deferrals 1..3 in 41 s
# against a gateway busy with two review chats filed a repair card. Only a streak that outlives a
# drain cool-off (``MIN_STREAK_S``) files one.

BUSY = "hermes-gateway is busy or cannot acknowledge an idle pause; retry when idle"


def test_three_retries_in_41_seconds_file_nothing(tmp_path, monkeypatch, capsys, clock, reported):
    for at in (0, 13, 41):
        clock.at = at
        assert deferrals.note_deferral([UNIT], BUSY) == []
    reported.assert_not_called()
    assert "Repair owner" not in capsys.readouterr().out
    streak = _streak()
    assert streak["count"] == 3 and streak["first_at"] == T0.isoformat(timespec="seconds")


def test_the_same_streak_files_once_it_outlives_the_floor(tmp_path, monkeypatch, capsys, clock, reported):
    for at in (0, 13, 41, 120):
        clock.at = at
        deferrals.note_deferral([UNIT], BUSY)
    reported.assert_not_called()
    clock.at = deferrals.MIN_STREAK_S
    assert deferrals.note_deferral([UNIT], BUSY) == ["t_fixture_owner"]
    reported.assert_called_once()
    unit, entry = reported.call_args.args
    assert unit == UNIT and entry["count"] == 5 and entry["first_at"] == T0.isoformat(timespec="seconds")
    assert f"{UNIT} has deferred 5 updates in a row" in capsys.readouterr().out


def test_a_wall_over_hours_still_files_from_the_first_deferral_past_the_floor(tmp_path, monkeypatch, clock, reported):
    """2026-11-11 shape: eleven refusals over hours. Bursts of instant retries each followed by a
    10 min cool-off; the first deferral 15 min or more into the streak files, every later one keeps."""
    times = [0, 10, 30, 70, 670, 680, 700, 740, 3600, 7200, 10800]
    filed_at = []
    for i, at in enumerate(times, 1):
        clock.at = at
        if deferrals.note_deferral([UNIT], "does not support idle pause"):
            filed_at.append((i, at))
    assert filed_at[0] == (9, 3600), "the first deferral past the floor did not file"
    assert [c.args[1]["count"] for c in reported.call_args_list] == [i for i, _ in filed_at]
    assert [i for i, _ in filed_at] == list(range(filed_at[0][0], 12))


def test_a_pause_that_goes_through_restarts_the_floor(tmp_path, monkeypatch, clock, reported):
    for at in (0, 10, 20):
        clock.at = at
        deferrals.note_deferral([UNIT], BUSY)
    deferrals.note_pause_ok()
    for at in (1000, 1010, 1020):  # past T0 + floor, but a fresh streak
        clock.at = at
        assert deferrals.note_deferral([UNIT], BUSY) == []
    reported.assert_not_called()
    assert _streak()["first_at"] == (T0 + timedelta(seconds=1000)).isoformat(timespec="seconds")
    clock.at = 1000 + deferrals.MIN_STREAK_S
    assert deferrals.note_deferral([UNIT], BUSY) == ["t_fixture_owner"]
    assert reported.call_args.args[1]["count"] == 4


def test_an_unreadable_first_at_fails_towards_filing(tmp_path, monkeypatch, clock, reported):
    deferrals.state_path().parent.mkdir(parents=True, exist_ok=True)
    deferrals.state_path().write_text(json.dumps({"units": {UNIT: {
        "count": 4, "first_at": "not-a-time", "last_at": "not-a-time", "reason": BUSY}}}))
    assert deferrals.note_deferral([UNIT], BUSY) == ["t_fixture_owner"]
    unit, entry = reported.call_args.args
    assert unit == UNIT and entry["count"] == 5 and entry["first_at"] == "not-a-time"
