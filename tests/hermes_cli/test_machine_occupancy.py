"""Whole shared-board root occupancy is independent of active profile and sync filters."""
import sqlite3

from hermes_cli import machine_metrics as metrics


def board(path, rows, own="worker-1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE tasks(id TEXT, status TEXT, claim_lock TEXT, lease_holder TEXT);"
                         "CREATE TABLE kanban_sync_state(gateway_id TEXT);")
        db.execute("INSERT INTO kanban_sync_state VALUES(?)", (own,))
        db.executemany("INSERT INTO tasks VALUES(?,?,?,?)", rows)


def test_occupancy_counts_all_profiles_and_named_boards_but_not_foreign_leases(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    board(tmp_path / "kanban.db", [("a", "running", "pid-a", None), ("b", "running", None, "hub"),
                                   ("c", "running", None, "worker-1"), ("d", "ready", None, None)])
    board(tmp_path / "kanban/boards/research/kanban.db", [("e", "running", "pid-e", None)])
    # A shadow snapshot is not execution; counting it would count every remote run twice.
    board(tmp_path / "kanban/shadow/default.db", [("e", "running", "pid-e", None)])
    assert metrics.active_card_runs() == 3


def test_corrupt_board_makes_occupancy_unknown_not_zero(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    board(tmp_path / "kanban.db", [])
    bad = tmp_path / "kanban/boards/bad/kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_text("not SQLite")
    assert metrics.active_card_runs() is None


def test_collect_reports_unknown_as_null_to_clear_old_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "active_card_runs", lambda: None, raising=False)
    assert metrics.collect(tmp_path)["active_card_runs"] is None
    monkeypatch.setattr(metrics, "active_card_runs", lambda: 7)
    sent = []
    assert metrics.Reporter(sent.append).report_due()
    assert sent[0]["active_card_runs"] == 7
