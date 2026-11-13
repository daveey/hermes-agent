"""A repeated completion of the same card adds its occurrence to the detector card once. [daveey]

PR #2329 post-merge mutant A19 (card t_01a1d5e5bf3ccad9): ``note_completion`` checks for the card's comment
before adding one, and no test completed a card twice, so dropping that check left the suite green while a
reopened-and-recompleted duplicate posted the same line again on every completion.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dup_detector as dd


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


def test_the_same_card_noted_twice_is_commented_once(conn):
    creator = kb.create_task(conn, title="creator", assignee="default")
    first, second, third = (kb.create_task(conn, title=t, assignee="default", creator_task_id=creator)
                            for t in ("first", "second", "third"))
    card = dd.note_completion(conn, second, f"duplicate of {first}")
    assert card is not None
    for _ in range(3):  # the third card completes, is reopened and completes again
        assert dd.note_completion(conn, third, f"duplicate of {first}") == card
    comments = [c.body for c in kb.list_comments(conn, card)]
    assert len(comments) == 1 and comments[0].startswith(f"- {third} closed as a duplicate of {first};")


def test_the_card_named_in_the_body_completing_again_adds_no_comment(conn):
    """The first occurrence lives in the detector card's body, not a comment (card t_01a1d639077fda84): its card
    completing again must not post the same line as a comment."""
    creator = kb.create_task(conn, title="creator", assignee="default")
    first, second = (kb.create_task(conn, title=t, assignee="default", creator_task_id=creator)
                     for t in ("first", "second"))
    card = dd.note_completion(conn, second, f"duplicate of {first}")
    assert card is not None
    task = kb.get_task(conn, card)
    assert task is not None and f"\n- {second} closed as a duplicate of {first};" in (task.body or "")
    for _ in range(2):  # the second card is reopened and completes again
        assert dd.note_completion(conn, second, f"duplicate of {first}") == card
    assert kb.list_comments(conn, card) == []


def test_a_card_whose_id_only_appears_quoted_in_the_body_is_still_commented(conn):
    """Only an occurrence line counts as seen: an id quoted inside the first card's handoff is a new occurrence."""
    creator = kb.create_task(conn, title="creator", assignee="default")
    first, second, third = (kb.create_task(conn, title=t, assignee="default", creator_task_id=creator)
                            for t in ("first", "second", "third"))
    card = dd.note_completion(conn, second, f"duplicate of {first}; see also {third}")
    assert card is not None and dd.note_completion(conn, third, f"duplicate of {first}") == card
    comments = [c.body for c in kb.list_comments(conn, card)]
    assert len(comments) == 1 and comments[0].startswith(f"- {third} closed as a duplicate of {first};")
