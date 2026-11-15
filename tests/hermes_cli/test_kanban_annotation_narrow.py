"""Display/holder-only narrow origin key: liveness churn does not move it; substantive changes do."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_annotation_narrow as narrow
from hermes_cli import kanban_session_activity as ksa

SCOPE = ("home", "prof", "kh", "hub", "mail")
LINK = {"session": "s1", "gw": "me", "profile": "prof", "rel": "shepherd"}
LINKED = "prof/main/t_linked"
OTHER = "zz/main/t_other"


def _task(tid, **kw):
    return json.dumps({"id": tid, "title": "x", "status": "running", **kw})


@pytest.fixture
def hub(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT,
        updated_at REAL, version INTEGER, seq INTEGER);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        INSERT INTO meta VALUES('gateway', 'me');
        INSERT INTO meta VALUES('last_ok', '1000');""")
    conn.execute("INSERT INTO replica VALUES('owner','tasks',?,?,1,1,1)",
                 (LINKED, _task("t_linked", sessions=[LINK], activity_at=1.0, activity_kind="tool")))
    conn.execute("INSERT INTO replica VALUES('owner','tasks',?,?,1,1,2)", (OTHER, _task("t_other")))
    info = {"gateway": "owner", "scope_heartbeat_period_s": 183,
            "task_scopes": [{"board": "main", "observed_at": 1000.0, "profile": "prof", "state": "ok"}]}
    conn.execute("INSERT INTO replica VALUES('owner','gateway','info',?,1,1,3)", (json.dumps(info),))
    conn.commit()
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: path)
    monkeypatch.setattr(narrow, "_ledger_path", lambda: tmp_path / "absent-ledger.db")
    narrow.reset()
    state = {"seq": 10}

    def write(sql, args=()):
        state["seq"] += 1
        conn.execute(sql.replace(":seq", str(state["seq"])), args)
        conn.commit()

    yield write
    narrow.reset()
    conn.close()


def key():
    return narrow.key(SCOPE, ("hubsig", ("board", 1)), [])


def test_liveness_only_change_keeps_key(hub):
    before = key()
    assert before is not None
    hub("UPDATE replica SET data=?, seq=:seq, version=2 WHERE key=?",
        (_task("t_linked", sessions=[LINK], activity_at=99.0, activity_kind="reply"), LINKED))
    assert key() == before


def test_substantive_linked_change_moves_key(hub):
    before = key()
    hub("UPDATE replica SET data=?, seq=:seq, version=2 WHERE key=?",
        (_task("t_linked", sessions=[LINK], activity_at=1.0, activity_kind="tool", status="done"), LINKED))
    assert key() != before


def test_unlinked_row_change_keeps_key(hub):
    before = key()
    hub("UPDATE replica SET data=?, seq=:seq, version=2 WHERE key=?", (_task("t_other", status="done"), OTHER))
    assert key() == before


def test_new_link_moves_key(hub):
    before = key()
    hub("UPDATE replica SET data=?, seq=:seq, version=2 WHERE key=?",
        (_task("t_other", sessions=[LINK]), OTHER))
    assert key() != before


def test_freshness_only_as_verdict_flips(hub, monkeypatch):
    """Clocks enter as freshness verdicts at now: an advancing clock never moves the key, a flip does. (180 s
    buckets of each owner's clock moved it once per bucket per owner: every ~16 s with ~11 owners on the hub.)"""
    now = {"t": 1100.0}
    monkeypatch.setattr(narrow.time, "time", lambda: now["t"])
    before = key()  # last_ok 1000: fresh (100 s); heartbeat 1000: fresh in a >= 450 s window
    hub("UPDATE meta SET v='1090' WHERE k='last_ok'")
    hub("UPDATE replica SET data=json_set(data,'$.task_scopes[0].observed_at',1095.0), seq=:seq "
        "WHERE coll='gateway'")
    hub("UPDATE replica SET data=json_set(data,'$.scope_heartbeat_period_s',184), seq=:seq WHERE coll='gateway'")
    now["t"] = 1200.0  # crosses the old 180 s bucket boundary (1080) for both clocks
    assert key() == before
    now["t"] = 1300.0  # last_ok 1090 is 210 s old: stale
    stale = key()
    assert stale != before
    hub("UPDATE meta SET v='1290' WHERE k='last_ok'")
    assert key() == before  # fresh again
    now["t"] = 1095.0 + 2000  # the heartbeat ages out of its window (last_ok kept fresh)
    hub("UPDATE meta SET v=? WHERE k='last_ok'", (str(now["t"] - 1),))
    assert key() not in (before, stale)
    hub("UPDATE replica SET data=json_set(data,'$.task_scopes[0].state','error'), seq=:seq WHERE coll='gateway'")
    aged = key()
    hub("UPDATE replica SET data=json_set(data,'$.task_scopes[0].state','ok'), seq=:seq WHERE coll='gateway'")
    assert key() != aged  # other info fields stay verbatim


def test_waiting_code_identity_moves_key_hub_signature_does_not(hub):
    base = key()
    assert narrow.key(SCOPE, ("hubsig", ("board", 1)), [["src", "x", 1]]) != base
    assert narrow.key(SCOPE, ("other-hubsig", ("board", 1)), []) == base  # hub signature is replaced
    assert narrow.key(SCOPE, ("hubsig", ("board", 2)), []) == base  # a missing board reads nothing either way


def test_unreadable_row_is_unknown_never_stale(hub):
    assert key() is not None
    hub("UPDATE replica SET data='{not json', seq=:seq WHERE key=?", (LINKED,))
    assert key() is None
    assert narrow.stats["errors"] >= 1


def test_offline_flag_moves_key(hub):
    before = key()
    hub("INSERT INTO meta VALUES('offline_since', '1005')")
    assert key() != before
