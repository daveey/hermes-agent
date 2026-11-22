"""A Board gateway renamed in place (``mini`` -> ``daveey-mini-1``): core follows the name. [daveey]

One test per entry path (t_01a204733c506aa0):
* ``gateway_name()`` / ``gateway_names()`` pick the new name up with no process restart;
* ``gateway_names()`` stops listing the old partition as a live gateway;
* ``hermes kanban wait-check task <id> --gateway <former>`` and ``wait-comment --from-gateway <former>``
  through the argparse entry;
* an origin ref written before the rename;
* the overlap host map.

The alias source is ``kanban.placement.aliases`` (config on disk), never the network.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_comment_origin as kco
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_gateway_alias as alias
from hermes_cli import kanban_origin as ko
from hermes_cli import kanban_overlap
from hermes_cli import kanban_wait_check as wait

OLD, NEW, OTHER = "mini", "daveey-mini-1", "hub"
TID = "t_123456789abcdef0"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "mail.json"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "hub-state").mkdir()
    ko._names_memo = ko._name_memo = None
    yield tmp_path
    ko._names_memo = ko._name_memo = None


def _old(path: Path) -> None:
    """Age ``path`` past the racy-write window, so its memo is kept as it is in a long-running process."""
    then = time.time() - 60
    os.utime(path, (then, then))


def hub_db(home, me: str, partitions=()) -> Path:
    """This host's hub.db: ``meta.gateway`` = ``me`` and one live replica record per partition."""
    path = home / "hub-state" / "hub.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
        db.execute("INSERT OR REPLACE INTO meta VALUES('gateway', ?)", (me,))
        db.execute("DELETE FROM replica")
        db.executemany("INSERT INTO replica VALUES(?, 'gateway', ?, '{}', 1)", [(g, g) for g in partitions])
    _old(path)
    return path


def mail_config(home, me: str) -> Path:
    path = home / "mail.json"
    path.write_text(json.dumps({"gateway": me, "url": "https://board.invalid"}), encoding="utf-8")
    _old(path)
    return path


def aliases(home, table: dict) -> None:
    (home / ".hermes" / "config.yaml").write_text(  # JSON is YAML
        json.dumps({"kanban": {"placement": {"aliases": table}}}), encoding="utf-8")


def rename_self(home, new: str) -> None:
    """What ``hermes-mail rename-self --to <new>`` leaves: the mail config and hub.db ``meta.gateway`` both
    carry the new name. Same hub.db file (inode), as an in-place rename keeps it."""
    with sqlite3.connect(home / "hub-state" / "hub.db") as db:
        db.execute("UPDATE meta SET v = ? WHERE k = 'gateway'", (new,))
    mail_config(home, new)


# --- name pickup ---------------------------------------------------------------------------------------------------

def test_gateway_name_follows_an_in_place_rename_without_a_restart(home):
    hub_db(home, OLD, [OLD, OTHER])
    mail_config(home, OLD)
    assert ko.gateway_name() == OLD
    assert ko._name_memo is not None, "the settled name must be memoized, or this test proves nothing"
    rename_self(home, NEW)
    assert ko.gateway_name() == NEW          # same process, no memo reset, well inside the 10-minute memo
    assert ko.gateway_names()[0] == NEW


def test_a_half_written_rename_is_not_memoized(home):
    """hub.db ``meta.gateway`` and the mail config disagree while ``rename-self`` is between its two writes:
    the name read then is returned but not kept, so the settled name is seen on the next call."""
    hub_db(home, OLD, [OLD])
    mail_config(home, NEW)                   # config written first; meta.gateway still the old name
    assert ko.gateway_name() == OLD
    assert ko._name_memo is None
    with sqlite3.connect(home / "hub-state" / "hub.db") as db:
        db.execute("UPDATE meta SET v = ? WHERE k = 'gateway'", (NEW,))
    assert ko.gateway_name() == NEW          # the mail config did not change again: only the no-memo rule helps
    assert ko._name_memo is not None


def test_a_settled_name_is_still_memoized(home, monkeypatch):
    """The memo's purpose survives: a list render asks once per card and must not open hub.db each time."""
    hub_db(home, OLD, [OLD])
    mail_config(home, OLD)
    assert ko.gateway_name() == OLD
    opened = []
    monkeypatch.setattr(ko, "_ro", lambda path: opened.append(path) or (_ for _ in ()).throw(AssertionError("opened")))
    assert [ko.gateway_name() for _ in range(5)] == [OLD] * 5 and not opened


# --- the old partition ---------------------------------------------------------------------------------------------

def test_gateway_names_drops_the_renamed_gateways_old_partition(home):
    """A pulled tombstone physically removes its replica row; config alone cannot establish retirement."""
    path = hub_db(home, OTHER, [OTHER, OLD, NEW])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW})
    assert ko.gateway_names() == (OTHER, {OTHER, OLD, NEW})
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM replica WHERE gw = ?", (OLD,))
    assert ko.gateway_names() == (OTHER, {OTHER, NEW})


def test_gateway_names_keeps_the_old_name_until_the_new_one_has_records(home):
    """The alias map is rolled out before the cutover: until the new name shows up here, ``mini`` is the
    gateway and must stay addressable."""
    hub_db(home, OTHER, [OTHER, OLD])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW})
    assert ko.gateway_names() == (OTHER, {OTHER, OLD})


def test_gateway_names_without_an_alias_map_is_unchanged(home):
    hub_db(home, OTHER, [OTHER, OLD, NEW])
    mail_config(home, OTHER)
    assert ko.gateway_names() == (OTHER, {OTHER, OLD, NEW})


def test_gateway_names_never_drops_this_gateways_own_name(home):
    """A wrong map (this host aliased to another known gateway) must not make the host forget its own name."""
    hub_db(home, OLD, [OLD, NEW])
    mail_config(home, OLD)
    aliases(home, {OLD: NEW})
    assert ko.gateway_names() == (OLD, {OLD, NEW})


def test_a_ref_written_before_the_rename_reaches_the_renamed_gateway(home):
    hub_db(home, OTHER, [OTHER, NEW])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW})
    got = ko.resolve(f"@session:default@{OLD}/20261122_040757_29014b")
    assert got == ko.Origin(NEW, "default", "20261122_040757_29014b")


def test_the_renamed_gateways_own_old_ref_is_local(home):
    hub_db(home, NEW, [NEW, OTHER])
    mail_config(home, NEW)
    aliases(home, {OLD: NEW})
    assert ko.resolve(f"@session:default@{OLD}/20261122_040757_29014b") is None


# --- the alias map -------------------------------------------------------------------------------------------------

def test_alias_chain_loop_and_case():
    cfg = {"aliases": {"Mac-Mini": "MINI", "mini": "daveey-mini-1", "a": "b", "b": "a", "same": "same", "": "x"}}
    assert alias.resolve("mac-mini", cfg) == NEW and alias.resolve("MINI", cfg) == NEW
    assert alias.resolve("worker-7", cfg) == "worker-7"
    assert alias.resolve("a", cfg) == "a"                      # a loop resolves to nothing new
    assert alias.aliases(cfg) == {"mac-mini": "mini", "mini": NEW, "a": "b", "b": "a"}
    assert alias.aliases({"aliases": ["mini"]}) == {} and alias.aliases({}) == {}
    assert alias.current("mini", {"Daveey-Mini-1", "hub"}, cfg) == "Daveey-Mini-1"
    assert alias.current("mini", {"hub"}, cfg) == "mini"
    assert alias.current("hub", {"hub", NEW}, cfg) == "hub"
    assert alias.current("mac-mini", {"mini", NEW}, cfg) == "mac-mini"  # ambiguous, neither is preferred
    assert alias.current("MINI", {"mini", NEW}, cfg) == "mini"  # explicit partition survives
    assert alias.spellings(NEW, cfg) == [NEW, "mac-mini", "mini"]
    assert alias.spellings("mini", cfg) == ["mini", NEW, "mac-mini"]
    assert alias.spellings("worker-7", cfg) == ["worker-7"] and alias.spellings("", cfg) == []


# --- wait-check ----------------------------------------------------------------------------------------------------

def _cli(*argv: str) -> int:
    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="cmd"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def registry(home, gw: str, status: str) -> None:
    """The hermes-hub registry replica holds the card under gateway ``gw``, observed just now."""
    scope = {"task_scopes": [{"board": "default", "profile": "default", "state": "ok", "observed_at": time.time()}]}
    row = dict(id=TID, board="default", status=status, gateway=gw, profile="default", owner_profile="default",
               wait_observation={"parked_agent": False, "orphan_reason": None})
    with sqlite3.connect(home / "hub-state" / "hub.db") as db:
        db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (gw, "gateway", gw, json.dumps(scope), 1))
        db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (gw, "tasks", f"default/default/{TID}", json.dumps(row), 1))


@pytest.fixture
def no_ssh(monkeypatch):
    """No route to the owner (the pool-worker case): the probe answers from this host's replicas."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey).")

    monkeypatch.setattr(wait.subprocess, "run", run)
    return calls


def test_wait_check_cli_with_a_former_gateway_name_reads_the_renamed_partition(home, no_ssh, capsys):
    hub_db(home, OTHER, [OTHER])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW})
    registry(home, NEW, "done")
    rc = _cli("wait-check", "task", TID, "--gateway", OLD)
    out = capsys.readouterr().out
    assert rc == 0 and out.split()[:2] == [TID, "done"], out
    assert [c[5] for c in no_ssh] == [OLD]   # the typed name is still the ssh destination


def test_wait_check_cli_with_a_former_name_is_not_met_while_the_card_runs(home, no_ssh, capsys):
    hub_db(home, OTHER, [OTHER])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW})
    registry(home, NEW, "running")
    assert _cli("wait-check", "task", TID, "--gateway", OLD) == 1
    assert "running" in capsys.readouterr().out


def test_wait_check_cli_keeps_the_old_name_before_the_rename(home, no_ssh, capsys):
    """Map present, rename not done: the owner's records are still under ``mini`` and are read there."""
    hub_db(home, OTHER, [OTHER])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW})
    registry(home, OLD, "done")
    assert _cli("wait-check", "task", TID, "--gateway", OLD) == 0
    assert capsys.readouterr().out.split()[:2] == [TID, "done"]


def test_wait_check_ssh_route_keeps_the_typed_host(home):
    """ssh names are their own namespace: host ``mini`` stays reachable as ``mini``; a name with no built-in
    route falls back to its current gateway's."""
    hub_db(home, OTHER, [OTHER, NEW])
    mail_config(home, OTHER)
    aliases(home, {OLD: NEW, "the-hub": OTHER})
    assert wait._ssh_host(OLD) == OLD
    assert wait._ssh_host("the-hub") == wait.GATEWAY_SSH[OTHER]
    assert wait._ssh_host("worker-7") == "worker-7"


# --- wait-comment --------------------------------------------------------------------------------------------------

@pytest.fixture
def board(home):
    hub_db(home, OTHER, [OTHER, NEW])
    mail_config(home, OTHER)
    kb.init_db()
    return home


@pytest.mark.parametrize("author,origin,flag,want", [
    ("default", NEW, OLD, 0),                 # written after the rename, probe parked before it
    ("default", OLD, NEW, 0),                 # written before the rename, probe written after it
    (f"hub:gw:{OLD}", None, NEW, 0),          # the relay author form, old name
    ("default", OTHER, OLD, 1),               # another gateway's comment still does not match
    ("default", OTHER, NEW, 1),
])
def test_wait_comment_cli_matches_both_names_of_a_renamed_gateway(board, capsys, author, origin, flag, want):
    aliases(board, {OLD: NEW})
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.add_comment(conn, tid, author, "verification: fix live", origin_gateway=origin)
    assert _cli("wait-comment", tid, "--after-id", "0", "--from-gateway", flag) == want
    capsys.readouterr()


def test_wait_comment_cli_without_the_alias_does_not_match_the_other_name(board, capsys):
    """Control: the match above comes from the map, not from a substring (``x9`` / ``y7`` share none)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.add_comment(conn, tid, "default", "reply", origin_gateway="y7")
    assert _cli("wait-comment", tid, "--after-id", "0", "--from-gateway", "x9") == 1
    aliases(board, {"x9": "y7"})
    assert _cli("wait-comment", tid, "--after-id", "0", "--from-gateway", "x9") == 0
    capsys.readouterr()


def test_wait_comment_from_card_and_gateway_matches_the_renamed_gateway(board):
    aliases(board, {"x9": "y7"})
    card = "t_0123456789abcdef"
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.add_comment(conn, tid, f"y7/{card}", "reply")
        assert kco.find_comment(conn, tid, from_card=card, from_gateway="x9") is not None
        assert kco.find_comment(conn, tid, from_card=card, from_gateway="z3") is None


# --- overlap host map ----------------------------------------------------------------------------------------------

def test_overlap_host_map_knows_the_new_gateway_name_as_the_same_machine():
    """Two cards naming the M6 by its old and its new gateway name target one machine."""
    host = lambda title: kanban_overlap.target_identity(title)["host"]
    assert host("restart the gateway on daveey-mini-1") == host("restart the gateway on mini") == {"mini"}
    for name in ("daveey-mini", "mac-mini", "davids-mac-mini-1"):   # the existing names still mean that machine
        assert host(f"check {name}") == {"mini"}, name
    assert host("check hermes-hub") == {"hub"} and host("check mbp") == {"macbook"}
    assert host("tune the mini-batch size") == frozenset()
