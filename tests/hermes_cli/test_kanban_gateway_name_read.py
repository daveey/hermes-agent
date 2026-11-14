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


def test_a_meta_change_is_seen_on_the_next_call(hub, monkeypatch):
    db, state, _ = hub
    kanban_origin.gateway_names()  # a remembered answer for the old inputs
    assert kanban_origin.gateway_name() == "hub"
    db.execute("UPDATE meta SET v = 'renamed' WHERE k = 'gateway'")
    db.commit()
    _settle(state / "hub.db", state / "hub.db-wal")  # even a settled write changes the size/identity key
    assert kanban_origin.gateway_name() == "renamed"


def test_unchanged_inputs_are_reread_once_the_age_bound_passes(hub, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(kanban_origin.time, "monotonic", lambda: clock[0])
    kanban_origin.gateway_names()
    seen = _statements(monkeypatch)
    clock[0] += kanban_origin._NAMES_MAX_AGE_S - 0.1
    kanban_origin.gateway_name()
    assert seen == [], "re-read before the age bound"
    clock[0] += 0.2  # time alone, no input change
    kanban_origin.gateway_name()
    assert seen, "not re-read after the age bound"
