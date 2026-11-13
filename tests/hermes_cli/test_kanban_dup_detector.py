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


@pytest.mark.parametrize("summary", [
    "Duplicate check: not a duplicate of t_0123456789abcdef.",
    "Checked for duplicates: none, parent t_0123456789abcdef.",
    "Duplicates found: 0; parent t_0123456789abcdef owns the design.",
    "Superseded check: wasn't superseded by t_0123456789abcdef.",
])
def test_a_keyword_does_not_reach_past_a_later_denied_keyword_or_a_none(summary):
    """PR #2429 review: the forward window of an undenied keyword stops at the next keyword, and "none"/"0"
    right after a keyword denies it."""
    assert dd.sibling_named(summary) == []


@pytest.mark.parametrize("summary", [
    "No-op as duplicate of t_0123456789abcdef.",
    "Done with no changes as duplicate of t_0123456789abcdef.",
    "Closed with no further work, a duplicate of t_0123456789abcdef.",
    # PR #2481 review 5896093750: a not/never that negates another verb, or a "no" before "because".
    "Never started as duplicate of t_0123456789abcdef.",
    "Not run as duplicate of t_0123456789abcdef.",
    "no work because duplicate of t_0123456789abcdef.",
    "Not started because duplicate of t_0123456789abcdef.",
    "Wasn't run, as a duplicate of t_0123456789abcdef.",
])
def test_a_negator_that_does_not_govern_the_keyword_does_not_deny_it(summary):
    """PR #2429 review: "no" denies only "no duplicate(s)" / "not a duplicate", not "no-op" or "no <noun> as"."""
    assert dd.sibling_named(summary) == ["t_0123456789abcdef"]


@pytest.mark.parametrize("summary", [
    "Not a real duplicate of t_0123456789abcdef.",
    "No other duplicates, parent t_0123456789abcdef owns it.",
    "No known duplicates; parent t_0123456789abcdef.",
    "No open duplicates; see t_0123456789abcdef.",
    "No obvious duplicate of t_0123456789abcdef.",
    "Not an exact duplicate; parent t_0123456789abcdef.",
    "Not marked as duplicate of t_0123456789abcdef.",
    "Not marked as a duplicate of t_0123456789abcdef.",
    "Wasn't considered a duplicate of t_0123456789abcdef.",
    "Never flagged as duplicate of t_0123456789abcdef.",
    "Not closed as a duplicate of t_0123456789abcdef.",
    "Not being a duplicate of t_0123456789abcdef, it stays open.",
    "Checked for duplicates: none found, parent t_0123456789abcdef.",
    # PR #2535 review 5896481270: not/never right before "as" denies, as it did before #2535.
    "Closed, not as duplicate of t_0123456789abcdef.",
    "Not as duplicate of t_0123456789abcdef.",
    "Never as a duplicate of t_0123456789abcdef.",
])
def test_a_negator_two_words_before_the_keyword_still_denies_it(summary):
    """PR #2481 review: a hedge or adjective between the negator and the keyword keeps the denial."""
    assert dd.sibling_named(summary) == []


@pytest.mark.parametrize("summary", [
    "Closed as duplicate (not superseded) of t_0123456789abcdef.",
    "Duplicate: none other than t_0123456789abcdef.",
    "Closed (duplicate of t_0123456789abcdef).",
])
def test_an_aside_or_a_none_that_does_not_end_the_clause_keeps_the_card(summary):
    """PR #2481 review: a keyword-only aside is not a later keyword, and "none other than" is not a count."""
    assert dd.sibling_named(summary) == ["t_0123456789abcdef"]


def test_a_closed_detector_card_lets_the_next_occurrence_open_a_new_one(conn):
    creator = kb.create_task(conn, title="creator", assignee="default")
    first, second = _pair(conn, creator)
    assert kb.complete_task(conn, second, summary=f"duplicate of {first}")
    [card] = dd.describe(conn)
    assert kb.complete_task(conn, card["id"], summary="tuned the guard")
    third = kb.create_task(conn, title="third", assignee="default", creator_task_id=creator)
    assert kb.complete_task(conn, third, summary=f"Superseded by {first}.")
    assert len(dd.describe(conn)) == 2


def test_a_negator_ending_the_previous_line_does_not_deny_the_next_line():
    """A newline ends the clause: the "not" of "Tests: not run" does not deny the duplicate on the next line."""
    assert dd.sibling_named("Tests: not run\nDuplicate of t_0123456789abcdef.") == ["t_0123456789abcdef"]


def test_never_denies_the_keyword():
    assert dd.sibling_named("Never a duplicate of t_0123456789abcdef.") == []


def test_a_contraction_with_a_typographic_apostrophe_denies_the_keyword():
    """U+2019, as pasted from rendered markdown, denies like the ASCII apostrophe."""
    assert dd.sibling_named("Isn\u2019t a duplicate of t_0123456789abcdef.") == []


@pytest.mark.parametrize("summary", [
    "Not started since duplicate of t_0123456789abcdef.",
    "Never run so duplicate of t_0123456789abcdef.",
    "Not done but duplicate of t_0123456789abcdef.",
    "No work due duplicate of t_0123456789abcdef.",
    "Not started hence duplicate of t_0123456789abcdef.",
    "Not run thus duplicate of t_0123456789abcdef.",
    "Not run with duplicate of t_0123456789abcdef.",
])
def test_every_joining_word_stops_the_negator_reaching_the_keyword(summary):
    """Post-merge check of PR #2535: each ``_JOIN`` word means the negator governs another word, not the keyword.
    ``as`` and ``because`` are pinned by the not-denied cases earlier in this file."""
    assert dd.sibling_named(summary) == ["t_0123456789abcdef"]


@pytest.mark.parametrize("verb", [
    "marked", "considered", "flagged", "treated", "closed", "counted", "filed", "labeled", "labelled", "tagged",
    "reported", "listed", "deemed", "regarded", "classified", "recorded", "identified", "confirmed", "resolved",
    "seen",
])
@pytest.mark.parametrize("template", ["Not {} as a duplicate of t_0123456789abcdef.",
                                      "Never {} as duplicate of t_0123456789abcdef."])
def test_every_judging_verb_after_a_negator_denies_the_keyword(verb, template):
    """Post-merge check of PR #2535: each ``_JUDGED`` verb keeps the denial across "as"."""
    assert dd.sibling_named(template.format(verb)) == []


@pytest.mark.parametrize("summary", [
    "Not even marked a duplicate of t_0123456789abcdef.",
    "Never formally flagged the duplicate of t_0123456789abcdef.",
])
def test_negator_without_as_still_denies_the_duplicate(summary):
    """Post-merge check of PR #2577: "as" after the judging verb is optional. With a hedge word before the verb,
    the denial reaches the keyword only through that branch, since the plain two-word window is too short."""
    assert dd.sibling_named(summary) == []
