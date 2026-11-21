"""``_gateway_name`` / ``kanban_origin.gateway_name``: this gateway's own name without the replica scan.

``subagent.roster`` (a 0.9 Hz Desktop poll) projects one observation per owner, each asking the gateway's own
name. On the hub the ``gateway_names`` memo misses about once a second (hermes-hub writes hub.db's WAL
continuously), and each miss scanned every replica row (``SELECT DISTINCT gw FROM replica``: 2.3 ms at 46k
rows) only to throw the known-gateway set away. [daveey]
"""
import json
import os
import sqlite3
import time

import pytest

from hermes_cli import kanban_db_sync, kanban_origin
from hermes_cli import kanban_execution_observations as keo


@pytest.fixture
def hub(tmp_path, monkeypatch):
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "mail.json"))
    monkeypatch.setattr(kanban_origin, "_names_memo", None)
    monkeypatch.setattr(kanban_origin, "_name_memo", None)
    monkeypatch.setattr(kanban_db_sync, "sync_config", dict)  # no kanban.sync.gateway: the hub path is live
    db = sqlite3.connect(state / "hub.db")
    db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, PRIMARY KEY (gw, coll, key))")
    db.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    db.executemany("INSERT INTO replica VALUES (?, 'tasks', ?)", [("macbook", str(i)) for i in range(50)])
    db.commit()
    _settle(state / "hub.db")
    yield db, state, tmp_path / "mail.json"
    db.close()


def _settle(*paths):
    old = time.time() - 60
    for p in paths:
        if os.path.exists(p):
            os.utime(p, (old, old))  # a settled file, as on a running host (never inside the racy window)


def _statements(monkeypatch):
    """Every SQL statement run on a hub.db connection opened by kanban_origin."""
    real, seen = kanban_origin._ro, []

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(kanban_origin, "_ro", traced)
    return seen


def test_gateway_name_never_scans_the_replica_table(hub, monkeypatch):
    seen = _statements(monkeypatch)
    assert keo._gateway_name() == "hub"
    assert seen, "hub.db was not read at all"
    assert not [s for s in seen if "replica" in s.lower()], seen


def test_gateway_name_matches_gateway_names_in_every_branch(hub, monkeypatch):
    db, state, mail = hub

    def both():
        monkeypatch.setattr(kanban_origin, "_names_memo", None)
        monkeypatch.setattr(kanban_origin, "_name_memo", None)
        name = kanban_origin.gateway_name()
        monkeypatch.setattr(kanban_origin, "_names_memo", None)
        return name, kanban_origin.gateway_names()[0]

    assert both() == ("hub", "hub")
    db.execute("UPDATE meta SET v = '  spaced  ' WHERE k = 'gateway'")  # truthy: no fallback, unstripped
    db.commit()
    assert both() == ("  spaced  ", "  spaced  ")
    assert keo._gateway_name() == "spaced"
    mail.write_text(json.dumps({"gateway": "mail-gw"}), encoding="utf-8")
    db.execute("DELETE FROM meta")  # no meta row: the mail config names the gateway
    db.commit()
    assert both() == ("mail-gw", "mail-gw")
    db.execute("DROP TABLE meta")  # unreadable meta (sqlite error): still the mail config
    db.commit()
    assert both() == ("mail-gw", "mail-gw")
    mail.write_text("not json", encoding="utf-8")
    assert both() == ("", "")
    db.close()
    (state / "hub.db").unlink()  # no hub.db at all
    mail.write_text(json.dumps({"gateway": "solo"}), encoding="utf-8")
    assert both() == ("solo", "solo")


def test_kanban_sync_gateway_still_wins(hub, monkeypatch):
    seen = _statements(monkeypatch)
    monkeypatch.setattr(kanban_db_sync, "sync_config", lambda: {"gateway": " cfg-gw "})
    assert keo._gateway_name() == "cfg-gw"
    assert seen == []


def test_a_valid_gateway_names_memo_answers_without_a_read(hub, monkeypatch):
    kanban_origin.gateway_names()  # settled inputs: remembered
    seen = _statements(monkeypatch)
    assert kanban_origin.gateway_name() == "hub"
    assert seen == []


def test_the_name_is_kept_across_hub_db_writes_and_dropped_when_hub_db_is_replaced(hub, monkeypatch):
    """Gateways are never renamed in place: the WAL/mtime the hub writes all the time must not bust the memo, and a
    re-enrollment (a new hub.db file) must."""
    db, state, _ = hub
    assert kanban_origin.gateway_name() == "hub"
    seen = _statements(monkeypatch)
    for i in range(20):  # the hub's continuous writes: WAL and main file change size/mtime
        db.execute("INSERT INTO replica VALUES ('mini', 'tasks', ?)", (f"w{i}",))
        db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("UPDATE meta SET v = 'in-place' WHERE k = 'gateway'")
    db.commit()
    _settle(state / "hub.db", state / "hub.db-wal")
    assert kanban_origin.gateway_name() == "hub"
    assert seen == [], "hub.db was reopened although its file identity did not change"
    db.close()
    fresh = state / "hub.db.new"  # re-enrollment: a new file renamed over the old one (a new inode)
    new = sqlite3.connect(fresh)
    new.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    new.execute("INSERT INTO meta VALUES ('gateway', 're-enrolled')")
    new.commit()
    new.close()
    os.replace(fresh, state / "hub.db")
    _settle(state / "hub.db")
    assert kanban_origin.gateway_name() == "re-enrolled"


def test_a_mail_config_change_is_seen_on_the_next_call(hub, monkeypatch):
    db, _state, mail = hub
    db.execute("DELETE FROM meta")
    db.commit()
    mail.write_text(json.dumps({"gateway": "one"}), encoding="utf-8")
    _settle(mail)
    assert kanban_origin.gateway_name() == "one"
    mail.write_text(json.dumps({"gateway": "two!"}), encoding="utf-8")
    _settle(mail)
    assert kanban_origin.gateway_name() == "two!"


def test_unchanged_inputs_are_reread_once_the_ten_minute_bound_passes(hub, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(kanban_origin.time, "monotonic", lambda: clock[0])
    assert kanban_origin._NAME_MAX_AGE_S == 600.0
    kanban_origin.gateway_name()
    seen = _statements(monkeypatch)
    clock[0] += kanban_origin._NAME_MAX_AGE_S - 0.1
    kanban_origin.gateway_name()
    assert seen == [], "re-read before the age bound"
    clock[0] += 0.2  # time alone, no input change
    kanban_origin.gateway_name()
    assert seen, "not re-read after the age bound"


def test_a_failed_hub_db_read_is_not_remembered(hub, monkeypatch):
    real, calls = kanban_origin._ro, []

    def flaky(path):
        calls.append(str(path))
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(path)

    monkeypatch.setattr(kanban_origin, "_ro", flaky)
    assert kanban_origin.gateway_name() == ""  # no mail config to fall back on
    assert kanban_origin.gateway_name() == "hub"
    assert kanban_origin.gateway_name() == "hub"
    assert len(calls) == 2


def test_an_empty_name_is_not_remembered(hub, monkeypatch):
    """hermes-hub writes meta.gateway only after its first sync: an empty answer must not stick for 10 min."""
    db, state, _ = hub
    db.execute("UPDATE meta SET v = '' WHERE k = 'gateway'")  # an empty meta row (and no mail config)
    db.commit()
    _settle(state / "hub.db", state / "hub.db-wal")
    assert kanban_origin.gateway_name() == ""
    assert kanban_origin._name_memo is None
    db.execute("UPDATE meta SET v = 'hub' WHERE k = 'gateway'")  # the first sync lands, same hub.db file
    db.commit()
    _settle(state / "hub.db", state / "hub.db-wal")
    assert kanban_origin.gateway_name() == "hub"
