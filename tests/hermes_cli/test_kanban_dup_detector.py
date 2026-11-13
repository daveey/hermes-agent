"""Cards closed as a duplicate of their own creator's earlier card open one detector card. [daveey]

The three 11-13 completions (hub default board) are replayed with their recorded handoff summaries; only the
sibling ids are rewritten to the cards this test creates. The pattern opens exactly one card; the later two
occurrences become comments on it.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dup_detector as dd

RECORDED = [
    ("t_01a1d44c7b488318", "duplicate of t_01a1d44c7b488318"),
    ("t_01a1d4914914925a", "Closed as a duplicate of t_01a1d4914914925a, which delivered this exact scope in PR "
                           "#2140. That PR was merged into state as c01fa92d0a (I re-checked it this run). No code "
                           "changes were made on this card."),
    ("t_01a1d462ff5c8698", "Superseded: the same fix landed on main as 5b7202e08e from sibling card "
                           "t_01a1d462ff5c8698. It applies with_own_result in session_kanban_continue."
                           "_answer_interrupted_calls, and its test is stronger than mine. My equivalent PR #2237 "
                           "(RED on base / GREEN locally, check gate ok) hit a conflict on rebase against that commit, "
                           "so I closed it with a pointer and removed its branch and worktree."),
]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as c:
        yield c


def _pair(conn, creator):
    first = kb.create_task(conn, title="first", assignee="default", creator_task_id=creator)
    second = kb.create_task(conn, title="second", assignee="default", creator_task_id=creator)
    return first, second


def test_three_recorded_duplicate_completions_open_exactly_one_card(conn):
    opened = []
    for i, (recorded_sibling, summary) in enumerate(RECORDED):
        creator = kb.create_task(conn, title=f"creator {i}", assignee="default")
        first, second = _pair(conn, creator)
        assert kb.complete_task(conn, second, summary=summary.replace(recorded_sibling, first))
        opened.append(first)
    cards = dd.describe(conn)
    assert len(cards) == 1 and cards[0]["status"] in ("ready", "todo")
    card = kb.get_task(conn, cards[0]["id"])
    assert card is not None and card.title == dd.TITLE and "both created by" in (card.body or "")
    comments = [c.body for c in kb.list_comments(conn, card.id)]
    assert len(comments) == 2 and all("closed as a duplicate of" in c for c in comments)
    record = "\n".join([card.body or "", *comments])
    assert all(f"a duplicate of {first};" in record for first in opened)


def test_other_creator_and_plain_completions_open_nothing(conn):
    a = kb.create_task(conn, title="creator a", assignee="default")
    b = kb.create_task(conn, title="creator b", assignee="default")
    theirs = kb.create_task(conn, title="theirs", assignee="default", creator_task_id=a)
    mine = kb.create_task(conn, title="mine", assignee="default", creator_task_id=b)
    plain = kb.create_task(conn, title="plain", assignee="default", creator_task_id=b)
    assert kb.complete_task(conn, mine, summary=f"duplicate of {theirs}")  # another creator: the race, not this
    assert kb.complete_task(conn, plain, summary=f"Landed; reviewed by {theirs}.")
    assert dd.describe(conn) == []


@pytest.mark.parametrize("summary", [
    "Not a duplicate of {first}: this card fixes the Mac path, that one the hub path.",
    "No duplicates found; parent {first} owns only the design.",
    "This isn't a duplicate of {first}, which covers the other module.",
    "Checked for a non-duplicate scope against {first}.",
])
def test_a_completion_that_denies_duplication_opens_nothing(conn, summary):
    creator = kb.create_task(conn, title="creator", assignee="default")
    first, second = _pair(conn, creator)
    assert dd.sibling_named(summary.format(first=first)) == []
    assert kb.complete_task(conn, second, summary=summary.format(first=first))
    assert dd.describe(conn) == []


def test_a_negation_in_an_earlier_clause_does_not_hide_a_duplicate():
    assert dd.sibling_named("No code changes; duplicate of t_0123456789abcdef.") == ["t_0123456789abcdef"]
    assert dd.sibling_named("Not a duplicate of t_0123456789abcdef; superseded by t_fedcba9876543210.") == [
        "t_fedcba9876543210"]
    assert dd.sibling_named("No duplicates elsewhere, superseded by t_fedcba9876543210.") == ["t_fedcba9876543210"]


def test_a_closed_detector_card_lets_the_next_occurrence_open_a_new_one(conn):
    creator = kb.create_task(conn, title="creator", assignee="default")
    first, second = _pair(conn, creator)
    assert kb.complete_task(conn, second, summary=f"duplicate of {first}")
    [card] = dd.describe(conn)
    assert kb.complete_task(conn, card["id"], summary="tuned the guard")
    third = kb.create_task(conn, title="third", assignee="default", creator_task_id=creator)
    assert kb.complete_task(conn, third, summary=f"Superseded by {first}.")
    assert len(dd.describe(conn)) == 2
