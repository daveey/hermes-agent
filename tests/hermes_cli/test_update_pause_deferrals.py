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


# --- Sibling hosts (2026-11-12) ---------------------------------------------------------------
#
# worker-5, worker-1 and worker-6 share the install root and the unit, so their alarms had ONE key,
# and each filed its own card for one fleet-wide refusal: three investigations, two competing PRs.
# The key names the host; an open alarm on another host for the same refusal is linked.

FLEET = "hermes-gateway"
FLEET_BUSY = "hermes-gateway is busy or cannot acknowledge an idle pause; retry when idle"
DRAINING = "hermes-gateway still draining after 30s; retry once in-flight turns finish"


def _as_host(monkeypatch, host):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: host)


def _file(monkeypatch, host, reason=FLEET_BUSY, count=3):
    from hermes_cli.kanban_db_connect import connect_closing
    _as_host(monkeypatch, host)
    with connect_closing() as conn:
        return deferrals._report(conn, FLEET, count, "2026-11-12T15:17:00+00:00", reason)


def _card(task_id):
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        row = conn.execute("SELECT body, idempotency_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
        notes = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND author = 'update-pause' ORDER BY id",
            (task_id,))]
    return row[0], row[1], notes


def test_a_two_hosts_with_one_install_root_and_unit_get_their_own_cards(tmp_path, monkeypatch):
    """(a) The key names the host: worker-1's alarm is never folded into worker-5's card."""
    _board(tmp_path, monkeypatch)
    w5 = _file(monkeypatch, "worker-5")
    w1 = _file(monkeypatch, "worker-1")
    assert w1 != w5, "worker-1's alarm was folded into worker-5's card"
    assert _card(w1)[1] != _card(w5)[1], "two hosts produced one idempotency key"
    assert "On `worker-1`" in _card(w1)[0]
    assert _file(monkeypatch, "worker-1", count=4) == w1, "a host's own open card was not kept"


def test_b_second_host_names_the_open_sibling_and_comments_on_it(tmp_path, monkeypatch, clock):
    """(b) worker-1 hits worker-5's refusal: worker-5's card hears of worker-1 (host, time, refusal), and
    worker-1's card names worker-5's as the likely shared cause and parks on its fix first."""
    _board(tmp_path, monkeypatch)
    w5 = _file(monkeypatch, "worker-5")
    w1 = _file(monkeypatch, "worker-1")
    body = _card(w1)[0]
    assert f"Sibling alarm: {w5}" in body and "do not investigate from scratch" in body
    assert "park this card on it" in body
    notes = _card(w5)[2]
    assert len(notes) == 1 and "`worker-1`" in notes[0] and FLEET_BUSY in notes[0] and w1 in notes[0]
    assert T0.isoformat(timespec="seconds") in notes[0], "the sighting carries no time"
    # A third host joins the first investigation, not worker-1's follower card.
    w6 = _file(monkeypatch, "worker-6")
    assert f"Sibling alarm: {w5}" in _card(w6)[0]
    assert len(_card(w5)[2]) == 2 and _card(w1)[2] == []
    # worker-1's later deferrals keep its card and never re-comment the sibling.
    assert _file(monkeypatch, "worker-1", count=4) == w1 and len(_card(w5)[2]) == 2


def test_b_a_third_host_joins_the_lead_alarm_even_when_a_follower_sorts_first(tmp_path, monkeypatch):
    """created_at has one-second resolution and ids are random: a follower filed in the same second can
    sort before the alarm it follows. A third host must still join the lead (the alarm naming no open
    sibling), never the follower. Pinned here by making the follower look older."""
    from hermes_cli.kanban_db_connect import connect_closing
    _board(tmp_path, monkeypatch)
    w5 = _file(monkeypatch, "worker-5")
    w1 = _file(monkeypatch, "worker-1")
    assert f"Sibling alarm: {w5}" in _card(w1)[0]
    with connect_closing() as conn:
        conn.execute("UPDATE tasks SET created_at = (SELECT created_at FROM tasks WHERE id = ?) - 1 "
                     "WHERE id = ?", (w5, w1))
        conn.commit()
    w6 = _file(monkeypatch, "worker-6")
    assert f"Sibling alarm: {w5}" in _card(w6)[0], "worker-6 joined worker-1's follower card"
    assert _card(w1)[2] == []


def test_b_the_cli_child_entry_links_the_sibling(tmp_path, monkeypatch, capsys):
    """(b) via ``main`` (the child interpreter ``report`` spawns): same linking."""
    _board(tmp_path, monkeypatch)
    w5 = _file(monkeypatch, "worker-5")
    _as_host(monkeypatch, "worker-1")
    assert deferrals.main(["--unit", FLEET, "--count", "3", "--first-at", "2026-11-12T15:43:00+00:00",
                           "--reason", FLEET_BUSY]) == 0
    w1 = capsys.readouterr().out.strip().rsplit("=", 1)[1]
    assert w1 != w5 and f"Sibling alarm: {w5}" in _card(w1)[0] and w1 in _card(w5)[2][0]


def test_b_note_deferral_end_to_end_links_the_sibling(tmp_path, monkeypatch, clock):
    """(b) via ``note_deferral`` -> ``report`` -> a real child interpreter, running as this host. (The
    live-system guard refuses a child whose argv names the bare ``hermes-gateway`` unit: use ``UNIT``.)"""
    import socket

    from hermes_cli.kanban_db_connect import connect_closing
    _board(tmp_path, monkeypatch)
    reason = FLEET_BUSY.replace(FLEET, UNIT)
    with connect_closing() as conn:
        w5 = deferrals._report(conn, UNIT, 3, "2026-11-12T15:17:00+00:00", reason)
    conn_body = _card(w5)[0]
    assert f"On `{socket.gethostname()}`" in conn_body
    with connect_closing() as conn:  # the sibling was filed on another host
        conn.execute("UPDATE tasks SET body = replace(body, ?, ?), idempotency_key = ? WHERE id = ?",
                     (f"On `{socket.gethostname()}`", "On `elsewhere-host`", "update-pause-deferred:elsewhere", w5))
        conn.commit()
    filed = []
    for at in (0, 600, deferrals.MIN_STREAK_S):
        clock.at = at
        filed = deferrals.note_deferral([UNIT], reason)
    assert len(filed) == 1 and filed[0] != w5
    assert f"Sibling alarm: {w5}" in _card(filed[0])[0] and filed[0] in _card(w5)[2][0]


def test_b_a_foreign_sibling_in_the_board_replica_is_named_and_hears_from_its_home(tmp_path, monkeypatch):
    """Synced fleet: worker-5's card lives only in worker-1's Board replica (not writable there). worker-1's
    card names it; worker-5's own next deferral notes the sighting on its card, once."""
    import contextlib
    import sqlite3

    from hermes_cli import update_pause_siblings as sib
    from hermes_cli.kanban_db_connect import connect_closing

    w5_db, w1_db = tmp_path / "w5" / "board.db", tmp_path / "w1" / "board.db"
    for path in (w5_db, w1_db):
        path.parent.mkdir()

    def replica_of(path):
        @contextlib.contextmanager
        def seam(conn):
            rep = sqlite3.connect(path)
            try:
                yield rep
            finally:
                rep.close()
        return seam

    _board(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(w5_db))
    w5 = _file(monkeypatch, "worker-5")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(w1_db))
    monkeypatch.setattr(sib, "_replica", replica_of(w5_db))
    w1 = _file(monkeypatch, "worker-1")
    body = _card(w1)[0]
    assert f"Sibling alarm: {w5}" in body and '"action": "comment"' in body
    monkeypatch.setenv("HERMES_KANBAN_DB", str(w5_db))
    assert _card(w5)[2] == [], "worker-1 wrote on a card it holds only as a replica copy"
    monkeypatch.setattr(sib, "_replica", replica_of(w1_db))
    assert _file(monkeypatch, "worker-5", count=4) == w5
    assert _file(monkeypatch, "worker-5", count=5) == w5
    notes = _card(w5)[2]
    assert len(notes) == 1 and "`worker-1`" in notes[0] and w1 in notes[0] and FLEET_BUSY in notes[0]
    with connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_c_without_a_sibling_the_card_is_unchanged(tmp_path, monkeypatch):
    """(c) No open alarm on another host for this refusal: the ordinary card, no sibling text, no comments.
    Another host's alarm for a different refusal class, or a closed one, is no sibling."""
    from hermes_cli.kanban_db_connect import connect_closing
    _board(tmp_path, monkeypatch)
    other = _file(monkeypatch, "worker-5", reason=DRAINING)
    closed = _file(monkeypatch, "worker-6")
    with connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (closed,))
        conn.commit()
    lone = _file(monkeypatch, "worker-1")
    body = _card(lone)[0]
    assert "Sibling alarm" not in body and "Find why that gateway cannot take an idle pause" in body
    assert body.startswith("On `worker-1` (checkout ")
    assert _card(other)[2] == [] and _card(closed)[2] == []


def test_refusal_classes_ignore_pids_seconds_and_detail():
    from hermes_cli import update_pause_siblings as sib
    assert sib.refusal_class("gw-a", "gw-a still draining after 30s; retry") == \
        sib.refusal_class("gw-a", "gw-a still draining after 45s; retry")
    assert sib.refusal_class("gw-a", "could not inventory gw-a: Access denied") == \
        sib.refusal_class("gw-a", "could not inventory gw-a: Unit not loaded")
    assert sib.refusal_class(FLEET, FLEET_BUSY) != sib.refusal_class(FLEET, DRAINING)


def _busy(cause):
    return f"{FLEET} is busy or cannot acknowledge an idle pause [cause: {cause}]; retry when idle"


def test_refusal_classes_keep_the_head_of_the_busy_cause():
    """The real busy refusal carries ``[cause: ...]``: its head separates host failures, its detail does not."""
    from hermes_cli import update_pause_siblings as sib
    refused = _busy("gateway refused the update checkpoint: 2 turns running")
    timeout = _busy("did not answer the pause request (TimeoutError: timed out after 8.0s)")
    silent = _busy("gave no answer on its control socket")
    classes = {sib.refusal_class(FLEET, r) for r in (refused, timeout, silent, FLEET_BUSY)}
    assert len(classes) == 4, "different host failures were linked as one shared cause"
    assert sib.refusal_class(FLEET, refused) == \
        sib.refusal_class(FLEET, _busy("gateway refused the update checkpoint: 5 turns running"))
    assert sib.refusal_class(FLEET, timeout) == \
        sib.refusal_class(FLEET, _busy("did not answer the pause request (TimeoutError: timed out after 3s)"))
    # The card body keeps one line of the refusal: a multi-line reason classes alike on both sides.
    assert sib.refusal_class(FLEET, silent + "\nstderr: boom") == sib.refusal_class(FLEET, silent)


def test_c_another_cause_on_another_host_is_no_sibling(tmp_path, monkeypatch):
    """(c) Same unit, both busy, different causes: no link, no comment."""
    _board(tmp_path, monkeypatch)
    w5 = _file(monkeypatch, "worker-5", reason=_busy("gave no answer on its control socket"))
    w1 = _file(monkeypatch, "worker-1", reason=_busy("gateway refused the update checkpoint: busy"))
    assert "Sibling alarm" not in _card(w1)[0] and _card(w5)[2] == []


def test_a_host_that_enrolls_in_the_board_mid_streak_keeps_its_card(tmp_path, monkeypatch):
    """The key names the Board gateway id, else the hostname: gaining a gateway id mid-streak must not
    file a second card for the same host."""
    from hermes_cli import kanban_capability_routing as kcr
    _board(tmp_path, monkeypatch)
    before = _file(monkeypatch, "hermes-worker-1")
    monkeypatch.setattr(kcr, "_own_gateway", lambda conn: "worker-1")
    assert _file(monkeypatch, "hermes-worker-1", count=4) == before


def test_an_own_pre_host_key_card_with_an_empty_created_on_is_kept(tmp_path, monkeypatch):
    """An empty ``created_on`` is this board file's own card (as elsewhere in kanban)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import update_pause_siblings as sib
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.update_pause_record import install_root
    _board(tmp_path, monkeypatch)
    with connect_closing() as conn:
        old = kb.create_task(conn, title=sib.TITLE + FLEET, assignee="default", created_by="update-pause",
                             idempotency_key=sib.legacy_key(install_root(), FLEET), body="On `worker-1` ...")
        if "created_on" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")  # as a synced board has it
        conn.execute("UPDATE tasks SET created_on = '' WHERE id = ?", (old,))
        conn.commit()
    assert _file(monkeypatch, "worker-1") == old


def test_an_open_pre_host_key_card_of_this_host_is_kept(tmp_path, monkeypatch):
    """A streak open across the upgrade keeps its owner (legacy key)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import update_pause_siblings as sib
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.update_pause_record import install_root
    _board(tmp_path, monkeypatch)
    with connect_closing() as conn:
        old = kb.create_task(conn, title=sib.TITLE + FLEET, assignee="default", created_by="update-pause",
                             idempotency_key=sib.legacy_key(install_root(), FLEET), body="On `worker-1` ...")
    assert _file(monkeypatch, "worker-1") == old
