"""Kanban false signals: cross-gateway card ids are not "hallucinated"."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd
from tests.hermes_cli import kanban_sync_support as support

REMOTE_ID = "t_5bc8e125aa"
UNKNOWN_ID = "t_deadbeef00ff"


def _make_hub_db(state_dir, *, fresh=True, offline=False):
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_dir / "hub.db")
    conn.execute("CREATE TABLE replica (coll TEXT, gw TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO replica VALUES ('tasks', 'hub', 'default/default/' || ?, '{}', 0)", (REMOTE_ID,))
    conn.execute("INSERT INTO meta VALUES ('gateway', 'macbook')")
    conn.execute("INSERT INTO meta VALUES ('last_digest', ?)", (str(time.time() - (5 if fresh else 3600)),))
    if offline:
        conn.execute("INSERT INTO meta VALUES ('offline_since', ?)", (str(time.time()),))
    conn.commit()
    conn.close()


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hubstate"))
    conn = kbc.connect(home / "kanban.db")
    yield conn, tmp_path / "hubstate"
    conn.close()


def _complete_with_summary(conn, summary):
    tid = kb.create_task(conn, title="tracker", assignee="worker")
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, summary=summary)
    return tid


def _phantom_events(conn, tid):
    return [r for r in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = "
                                    "'suspected_hallucinated_references'", (tid,))]


def test_id_only_in_hub_replica_is_not_flagged_but_unknown_id_still_is(board):
    conn, hub = board
    _make_hub_db(hub)
    tid = _complete_with_summary(conn, f"see @task:default@hub/{REMOTE_ID} and {UNKNOWN_ID}")
    payloads = [r[0] for r in _phantom_events(conn, tid)]
    assert len(payloads) == 1
    assert UNKNOWN_ID in payloads[0] and REMOTE_ID not in payloads[0]


def test_replica_only_id_raises_no_event(board):
    conn, hub = board
    _make_hub_db(hub)
    assert _phantom_events(conn, _complete_with_summary(conn, f"hub card {REMOTE_ID}")) == []


def test_unknown_id_flagged_without_any_replica(board):
    conn, _ = board
    tid = _complete_with_summary(conn, f"ghost {UNKNOWN_ID}")
    assert len(_phantom_events(conn, tid)) == 1


HOME_BOARD_ID = "t_01a18d13cfd3dc82"  # a real card on the home board that this worker node never claimed


@pytest.fixture
def worker_node(tmp_path, monkeypatch):
    """A Board worker node: its boards sync to the Board service, it runs no hermes-hub sync (no ``hub.db``), and
    its live file holds only the cards it claimed."""
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hubstate"))
    support.write_sync_config(home, True)
    leases = support.lease_client()
    next(leases)
    conn = kbc.connect(home / "kanban.db")
    yield conn
    conn.close()
    leases.close()


def test_worker_node_summary_naming_unclaimed_home_board_card_is_not_flagged(worker_node):
    """(a) worker-1 completed t_01a18d195d03d5b7 naming t_01a18d13cfd3dc82, a real MacBook-board card it never
    copied; with no hub replica the scan used to call the local file the whole world and flag it."""
    conn = worker_node
    assert kb.get_task(conn, HOME_BOARD_ID) is None  # not in this node's partial view
    tid = _complete_with_summary(conn, f"the MacBook board shows {HOME_BOARD_ID} as archived")
    assert _phantom_events(conn, tid) == []
    events = kb.list_events(conn, tid)
    assert not [d for d in kd.compute_task_diagnostics(kb.get_task(conn, tid), events, [], now=int(time.time()))
                if d.kind == "prose_phantom_refs"]


def test_standalone_host_without_hub_still_flags_made_up_id(board, tmp_path):
    """(b) No Board sync and no hub replica: the local board is everything, so a made-up id is still flagged
    (and stays flagged in diagnostics)."""
    conn, hub = board
    support.write_sync_config(tmp_path / ".hermes", False)
    assert not hub.exists()
    tid = _complete_with_summary(conn, f"made up {UNKNOWN_ID}")
    payloads = [r[0] for r in _phantom_events(conn, tid)]
    assert len(payloads) == 1 and UNKNOWN_ID in payloads[0]
    events = kb.list_events(conn, tid)
    diags = kd.compute_task_diagnostics(kb.get_task(conn, tid), events, [], now=int(time.time()))
    assert [d.data["phantom_refs"] for d in diags if d.kind == "prose_phantom_refs"] == [[UNKNOWN_ID]]


@pytest.mark.parametrize("kw", [{"fresh": False}, {"offline": True}])
def test_stale_or_offline_replica_does_not_flag(board, kw):
    conn, hub = board
    _make_hub_db(hub, **kw)
    assert _phantom_events(conn, _complete_with_summary(conn, f"{REMOTE_ID} {UNKNOWN_ID}")) == []


def test_unreadable_replica_does_not_flag(board):
    conn, hub = board
    hub.mkdir(parents=True)
    (hub / "hub.db").write_bytes(b"not a database" * 100)
    assert _phantom_events(conn, _complete_with_summary(conn, UNKNOWN_ID)) == []


def test_diagnostic_re_resolves_old_warning_at_read_time(board):
    conn, hub = board
    tid = _complete_with_summary(conn, f"{REMOTE_ID} {UNKNOWN_ID}")  # no replica yet: both flagged
    events = kb.list_events(conn, tid)
    task = kb.get_task(conn, tid)
    diags = kd.compute_task_diagnostics(task, events, [], now=int(time.time()))
    assert [d.data["phantom_refs"] for d in diags if d.kind == "prose_phantom_refs"] == [[REMOTE_ID, UNKNOWN_ID]]
    _make_hub_db(hub)  # the replica now knows the hub card: the warning narrows, then clears
    diags = kd.compute_task_diagnostics(task, events, [], now=int(time.time()))
    assert [d.data["phantom_refs"] for d in diags if d.kind == "prose_phantom_refs"] == [[UNKNOWN_ID]]
    stale = hub / "hub.db"
    stale.unlink()
    _make_hub_db(hub, fresh=False)
    diags = kd.compute_task_diagnostics(task, events, [], now=int(time.time()))
    assert not [d for d in diags if d.kind == "prose_phantom_refs"]


