"""Spend sparing recognises a core PR by its `Hermes-Card:` ownership trailer (item 7)."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as spend
from tests.hermes_cli import test_kanban_spend_budget as helpers
from tests.hermes_cli import test_kanban_spend_pr_owner as base
from tests.hermes_cli.test_kanban_spend_pr_owner import PR, owner

kanban_home = helpers.kanban_home
github = base.github


def _stamped(conn, tid, body, *, writer="owner"):
    run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=? LIMIT 1", (tid,)).fetchone()[0]
    if writer == "foreign":
        other = owner(conn)
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=? LIMIT 1", (other,)).fetchone()[0]
    cid = kb.add_comment(conn, tid, "default", body)
    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
    if writer != "missing":
        kb._append_event(conn, tid, "commented", {
            "author": "default", "len": len(body), "comment_id": cid, "writer_run_id": run_id})


@pytest.mark.parametrize("wording", [
    "Item 7 part 1 of 2 (producer) published: softmax-universe/daveey-hermes PR #3598 ({pr}).",
    "Part 1 is up at {pr}; waiting on CI.",
    "Shipped {pr} (head abc123).",
    "Canonical publication advanced work: PR3598 OPEN at exact head.",
    "Published PR #3598; CI pending.",
    "PR 3598 is up.",
    "published pr#3598 and waiting",
])
def test_trailer_names_this_card_spares_any_owned_wording(kanban_home, github, wording):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        github[0]["body"] = f"Summary\n\nHermes-Card: {tid}\n"
        _stamped(conn, tid, wording.format(pr=PR))
        assert spend.hold(conn, tid) is None
        (event,) = helpers._events(conn, tid, "spend_budget_spared")
        assert event.payload["pr"] == PR
        assert helpers._events(conn, tid, spend.EVENT) == []
        assert len(github[1]) == 1


@pytest.mark.parametrize("trailer", [
    "Hermes-Card: t_other",
    "Hermes-Card: {tid}\nHermes-Card: t_other",
    "Hermes-Card: t_other\nHermes-Card: {tid}",
    "Hermes-Card: {tid}\nHermes-Card: {tid}",
    "Hermes-Card:",
    "Hermes-Card: {tid} (old)",
    "  Hermes-Card: {tid}",
    "> Hermes-Card: {tid}",
    "no marker at all",
    None,
])
def test_trailer_must_name_exactly_this_card(kanban_home, github, trailer):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        if trailer is None:
            github[0].pop("body", None)
        else:
            github[0]["body"] = "Summary\n\n" + trailer.format(tid=tid)
        _stamped(conn, tid, f"Opened {PR}")
        assert spend.hold(conn, tid) == "triage"
        assert helpers._events(conn, tid, "spend_budget_spared") == []


def test_trailer_accepts_crlf_body(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        github[0]["body"] = f"Summary\r\n\r\nHermes-Card: {tid}\r\n"
        _stamped(conn, tid, f"See {PR}")
        assert spend.hold(conn, tid) is None


@pytest.mark.parametrize("writer", ["foreign", "missing"])
def test_trailer_pr_in_unowned_comment_is_not_recognised(kanban_home, github, writer):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        github[0]["body"] = f"Hermes-Card: {tid}"
        _stamped(conn, tid, f"See {PR}", writer=writer)
        assert spend.hold(conn, tid) == "triage"


def test_quoted_pr_line_is_not_a_candidate(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        github[0]["body"] = f"Hermes-Card: {tid}"
        _stamped(conn, tid, f"> quoted from the reviewer: {PR}")
        assert spend.hold(conn, tid) == "triage"
        assert github[1] == []


@pytest.mark.parametrize("state,base", [("MERGED", "main"), ("CLOSED", "main"), ("OPEN", "state"), (None, "main")])
def test_trailer_owner_needs_open_main_pr(kanban_home, github, state, base):
    github[0].update(state=state, baseRefName=base)
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        github[0]["body"] = f"Hermes-Card: {tid}"
        _stamped(conn, tid, f"See {PR}")
        assert spend.hold(conn, tid) == "triage"


def test_below_budget_trailer_reads_nothing(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr", runs=14)
        _stamped(conn, tid, f"See {PR}")
        assert spend.hold(conn, tid) is None
        assert github[1] == []
