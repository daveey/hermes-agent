"""Rename overlap is not evidence that a placement alias points forward. [daveey]"""
import sqlite3

import pytest

from tests.hermes_cli import test_kanban_gateway_rename as helpers
from tests.hermes_cli.test_kanban_comment_origin import _park_on
from hermes_cli import kanban_comment_origin as kco

home = helpers.home
board = helpers.board
no_ssh = helpers.no_ssh


@pytest.mark.parametrize("reverse", [False, True])
def test_overlap_keeps_explicit_partition(home, no_ssh, capsys, reverse):
    old, new = helpers.OLD, helpers.NEW
    helpers.hub_db(home, helpers.OTHER, [helpers.OTHER])
    helpers.mail_config(home, helpers.OTHER)
    helpers.aliases(home, {new: old} if reverse else {old: new})
    helpers.registry(home, old, "done")
    helpers.registry(home, new, "running")
    assert {old, new} <= helpers.ko.gateway_names()[1]
    assert helpers.ko.resolve(f"@session:default@{new}/session").gateway == new
    assert helpers._cli("wait-check", "task", helpers.TID, "--gateway", new) == 1
    assert "running" in capsys.readouterr().out
    # No inference about direction from an independently arriving config update.
    assert helpers.wait.current_gateway(old) == old


@pytest.mark.parametrize("reverse", [False, True])
def test_tombstoned_partition_resolves_even_with_delayed_config(home, no_ssh, reverse):
    old, new = helpers.OLD, helpers.NEW
    helpers.hub_db(home, helpers.OTHER, [old, new])
    helpers.mail_config(home, helpers.OTHER)
    helpers.aliases(home, {new: old} if reverse else {old: new})
    assert old in helpers.ko.gateway_names()[1]
    with sqlite3.connect(home / "hub-state" / "hub.db") as conn:
        conn.execute("DELETE FROM replica WHERE gw = ?", (old,))
    helpers.registry(home, new, "running")
    assert old not in helpers.ko.gateway_names()[1]
    assert helpers._cli("wait-check", "task", helpers.TID, "--gateway", old) == 1
    assert helpers.wait.current_gateway(old) == new


@pytest.mark.parametrize("from_card", [False, True])
def test_alias_does_not_match_sibling_gateway(board, from_card):
    helpers.aliases(board, {helpers.OLD: helpers.NEW})
    card = "t_0123456789abcdef"
    author = f"daveey-mini-0/{card}" if from_card else "hub:gw:daveey-mini-0"
    with helpers.kbc.connect_closing() as conn:
        tid = helpers.kb.create_task(conn, title="probe", assignee="worker")
        helpers.kb.add_comment(conn, tid, author, "reply")
    args = ("--from-card", card) if from_card else ()
    assert helpers._cli("wait-comment", tid, "--after-id", "0", "--from-gateway", helpers.NEW, *args) == 1
    with helpers.kbc.connect_closing() as conn:
        author = f"{helpers.OLD}/{card}" if from_card else f"hub:gw:{helpers.OLD}"
        helpers.kb.add_comment(conn, tid, author, "real reply")
    assert helpers._cli("wait-comment", tid, "--after-id", "0", "--from-gateway", helpers.NEW, *args) == 0


def test_warning_alias_branch_and_sibling_negative(board):
    helpers.aliases(board, {"x9": "y7", helpers.OLD: helpers.NEW})
    with helpers.kbc.connect_closing() as conn:
        tid = helpers.kb.create_task(conn, title="warning probe", assignee="worker")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id 0 --from-gateway x9")
        assert kco.comment_wake_note(conn, tid, "default", "reply", origin_gateway="y7") is None
        assert "will NOT wake" in kco.comment_wake_note(conn, tid, "default", "reply", origin_gateway="z3")
        _park_on(conn, tid, f"hermes kanban wait-comment {tid} --after-id 0 --from-gateway {helpers.NEW}")
        assert "will NOT wake" in kco.comment_wake_note(conn, tid, "hub:gw:daveey-mini-0", "reply")
