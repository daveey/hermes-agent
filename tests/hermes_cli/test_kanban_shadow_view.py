"""A shadow-mode board lists and shows the replica's cards read-only; the replica is never written."""
from contextlib import closing
import json

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_shadow_view as shadow_view


def _replica_card(replica, title, *, home="mini", status=None, tid=None):
    tid = kb.create_task(replica, title=title, assignee="default")
    if status:
        replica.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    replica.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (home, tid))
    return tid


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A live board in ``mode`` plus its replica holding two mini cards and one id the live file also has."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True, "boards": ["default"]})
    kb.init_db()
    with closing(kbc.connect()) as live, live:
        live_id = kb.create_task(live, title="Hub card", assignee="default")
        sync.set_mode(live, "shadow")
    shadow = home / "kanban" / sync.SHADOW_DIR / "default.db"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    out = {"live_id": live_id, "shadow": shadow, "home": home}
    with closing(kbc.connect(shadow)) as replica, replica:
        if "created_on" not in {r[1] for r in replica.execute("PRAGMA table_info(tasks)")}:
            replica.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        out["ready_id"] = _replica_card(replica, "Mini ready", status="ready")
        out["done_id"] = _replica_card(replica, "Mini done", status="done")
        # Same id as the hub's own card, different title/status: the hub row must win.
        replica.execute("INSERT INTO tasks (id, title, status, created_at, created_on) VALUES (?, ?, 'ready', 1, 'mini')",
                        (live_id, "Mini copy of hub card"))
    return out


def _list_json(*args):
    return json.loads(kanban_cli.run_slash("list --json " + " ".join(args)))


def test_list_merges_replica_cards_marked_remote_read_only(board):
    rows = {r["id"]: r for r in _list_json()}
    assert board["ready_id"] in rows and board["live_id"] in rows
    assert board["done_id"] in rows  # `list` shows done cards; only archived are hidden
    mini = rows[board["ready_id"]]
    assert (mini["remote"], mini["read_only"], mini["home_gateway"]) == (True, True, "mini")
    assert "remote" not in rows[board["live_id"]]


def test_list_text_names_the_home_gateway(board):
    out = kanban_cli.run_slash("list")
    line = next(ln for ln in out.splitlines() if board["ready_id"] in ln)
    assert "[remote:mini, read-only]" in line
    hub = next(ln for ln in out.splitlines() if board["live_id"] in ln)
    assert "remote" not in hub


def test_list_filters_apply_to_replica_rows(board):
    ids = {r["id"] for r in _list_json("--status ready")}
    assert board["ready_id"] in ids and board["done_id"] not in ids


def test_hub_row_wins_on_id_collision(board):
    rows = [r for r in _list_json() if r["id"] == board["live_id"]]
    assert len(rows) == 1
    assert rows[0]["title"] == "Hub card" and "remote" not in rows[0]
    shown = json.loads(kanban_cli.run_slash(f"show {board['live_id']} --json"))
    assert shown["task"]["title"] == "Hub card" and "remote" not in shown["task"]


def test_show_reads_a_replica_only_card_read_only(board):
    shown = json.loads(kanban_cli.run_slash(f"show {board['ready_id']} --json"))
    assert shown["task"]["title"] == "Mini ready"
    assert (shown["task"]["remote"], shown["task"]["read_only"], shown["task"]["home_gateway"]) == (True, True, "mini")
    text = kanban_cli.run_slash(f"show {board['ready_id']}")
    assert "home gateway mini" in text


def test_missing_card_still_not_found(board):
    assert "no such task" in kanban_cli.run_slash("show t_nope")


@pytest.mark.parametrize("mode", ["primary", "off"])
def test_other_modes_do_not_merge(board, mode):
    with closing(kbc.connect()) as live, live:
        live.execute("UPDATE kanban_sync_state SET mode = ?", (mode,))
    assert {r["id"] for r in _list_json()} == {board["live_id"]}
    assert "no such task" in kanban_cli.run_slash(f"show {board['ready_id']}")


def test_sync_disabled_does_not_merge(board, monkeypatch):
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": False})
    assert {r["id"] for r in _list_json()} == {board["live_id"]}


def test_reading_never_writes_the_replica(board):
    shadow = board["shadow"]
    before, mtime = shadow.read_bytes(), shadow.stat().st_mtime_ns
    _list_json()
    _list_json("--archived")
    kanban_cli.run_slash("list")
    kanban_cli.run_slash(f"show {board['ready_id']}")
    kanban_cli.run_slash(f"show {board['ready_id']} --json")
    assert shadow.read_bytes() == before and shadow.stat().st_mtime_ns == mtime
    wal = shadow.with_name(shadow.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0  # no frame was ever appended


def test_replica_read_uses_a_read_only_connection(board):
    with closing(kbc.connect()) as live, shadow_view.open_shadow_replica(live) as replica:
        assert replica is not None
        with pytest.raises(Exception, match="readonly|read-only|read only"):
            replica.execute("UPDATE tasks SET title = 'x'")


def test_dispatcher_never_claims_a_shadow_row(board):
    spawned = []
    shadow = board["shadow"]
    before = shadow.read_bytes()
    with closing(kbc.connect()) as live:
        res = kbd.dispatch_once(live, spawn_fn=lambda *a, **k: spawned.append(a) or 1234)
        assert kb.get_task(live, board["ready_id"]) is None  # never imported into the live file
        assert kb.claim_task(live, board["ready_id"], claimer="worker") is None
        assert kb.claim_task(live, board["done_id"], claimer="worker") is None
    assert board["ready_id"] not in {row[0] for row in res.spawned}
    with closing(kbc.connect(shadow)) as replica:
        row = replica.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (board["ready_id"],)).fetchone()
    assert (row[0], row[1]) == ("ready", None)
    assert shadow.read_bytes() == before


def test_unknown_home_is_marked_in_list_and_show(board):
    with closing(kbc.connect(board["shadow"])) as replica, replica:
        replica.execute("UPDATE tasks SET created_on=NULL WHERE id=?", (board["ready_id"],))
    rows = {r["id"]: r for r in _list_json()}
    assert rows[board["ready_id"]]["home_gateway"] == "unknown"
    shown = json.loads(kanban_cli.run_slash(f"show {board['ready_id']} --json"))
    assert shown["task"]["home_gateway"] == "unknown"


def test_unreadable_replica_list_and_show_keep_live_rows(board, monkeypatch):
    import sqlite3
    original_list, original_get = kb.list_tasks, kb.get_task
    def listing(conn, **filters):
        if shadow_view.db_file(conn) == str(board["shadow"]):
            raise sqlite3.OperationalError("replica unavailable")
        return original_list(conn, **filters)
    def getting(conn, tid):
        if shadow_view.db_file(conn) == str(board["shadow"]):
            raise sqlite3.OperationalError("replica unavailable")
        return original_get(conn, tid)
    monkeypatch.setattr(kb, "list_tasks", listing)
    monkeypatch.setattr(kb, "get_task", getting)
    assert [r["id"] for r in _list_json()] == [board["live_id"]]
    assert "no such task" in kanban_cli.run_slash(f"show {board['ready_id']}")


@pytest.mark.parametrize("sort", list(kb.VALID_SORT_ORDERS))
def test_sort_applies_to_combined_rows(board, sort):
    with closing(kbc.connect()) as live, live:
        live.execute("UPDATE tasks SET created_at=30 WHERE id=?", (board["live_id"],))
    with closing(kbc.connect(board["shadow"])) as replica, replica:
        replica.execute("UPDATE tasks SET created_at=10 WHERE id=?", (board["ready_id"],))
        replica.execute("UPDATE tasks SET created_at=20 WHERE id=?", (board["done_id"],))
    with closing(kbc.connect(board["home"] / "oracle.db")) as oracle, oracle:
        with closing(kbc.connect()) as live:
            tasks = [(t, None) for t in kb.list_tasks(live)] + shadow_view.remote_tasks(live)
        for task, _home in tasks:
            oracle.execute("INSERT INTO tasks (id,title,status,assignee,priority,created_at,started_at,completed_at) "
                           "VALUES (?,?,?,?,?,?,?,?)", (task.id, task.title, task.status, task.assignee,
                                                      task.priority, task.created_at, task.started_at, task.completed_at))
        expected = [t.id for t in kb.list_tasks(oracle, order_by=sort)]
    actual = [r["id"] for r in _list_json(f"--sort {sort}")]
    assert actual == expected
    text = kanban_cli.run_slash(f"list --sort {sort}")
    assert sorted(expected, key=text.index) == expected
