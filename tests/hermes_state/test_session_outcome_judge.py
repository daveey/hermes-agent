"""A judge's verdict on the last assistant message survives a refresh in a process with no judge (CLI
``outcome-audit --refresh``, web ``?refresh=1``), and judges receive the row's ``source``."""

from __future__ import annotations

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB

# The heuristic reads this as a summary (long, no question, no progress opener)...
LOOKS_DONE = "Pushed the branch and opened the PR for the retry change; the reviewers are tagged on it."
# ...and this as not one (too short).
LOOKS_OPEN = "Done."


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ksa, "_board_db_paths", list)
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("chat", source="telegram")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()
    ksa._reset_cache_for_tests()


def _turn(db, reply):
    db.append_message("chat", role="user", content="ship the retry change")
    db.append_message("chat", role="assistant", content=reply)


@pytest.mark.parametrize("reply, verdict, kind", [(LOOKS_DONE, False, "unresolved"), (LOOKS_OPEN, True, "done")])
def test_a_judge_verdict_survives_a_refresh_without_a_judge(db, reply, verdict, kind):
    _turn(db, reply)
    so.register_outcome_judge(lambda sid, text, *, source=None: verdict)
    judged = so.refresh_outcome("chat", "turn_end", db=db)
    assert judged["kind"] == kind and judged["judged"] is True

    so.register_outcome_judge(None)  # a CLI / web refresh: no judge registered in that process
    assert so.refresh_outcome("chat", "manual", db=db)["kind"] == kind
    assert so.get_outcome("chat", db=db)["kind"] == kind


def test_a_new_assistant_message_is_judged_afresh(db):
    _turn(db, LOOKS_DONE)
    so.register_outcome_judge(lambda sid, text, *, source=None: False)
    assert so.refresh_outcome("chat", "turn_end", db=db)["kind"] == "unresolved"

    so.register_outcome_judge(None)
    _turn(db, LOOKS_DONE + " Also merged it.")
    out = so.refresh_outcome("chat", "manual", db=db)
    assert out["kind"] == "done" and "judged" not in out  # the heuristic decides a message no judge saw


def test_judges_get_the_source_and_old_judges_still_work(db):
    _turn(db, LOOKS_OPEN)
    seen = []
    so.register_outcome_judge(lambda sid, text, *, source=None: seen.append(source) or True)
    assert so.derive_outcome("chat", db=db)["kind"] == "done"
    assert seen == ["telegram"]

    so.register_outcome_judge(lambda sid, text: True)  # written before ``source`` was passed
    assert so.derive_outcome("chat", db=db)["kind"] == "done"
