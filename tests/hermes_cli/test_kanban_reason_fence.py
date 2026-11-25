"""Pre-insert fence contracts with fixture-owned SQLite, not live board state."""
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_reason_comment import (
    FencedReasonComment,
    RecordedDecisionComment,
    reason_comment,
)


@pytest.mark.parametrize("kind", ["none", "tuple", "recorded", "fenced"])
def test_reason_fence_runs_before_comment_and_can_be_absent(monkeypatch, kind):
    calls = []
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE tasks(id TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('fixture')")

        def before(actual):
            assert actual is conn
            assert calls == []
            calls.append("fence")

        def add(actual, task, author, body, **extra):
            assert actual is conn
            assert task == "fixture"
            assert (author, body) == ("fixture-author", "fixture-body")
            assert calls == (["fence"] if kind == "fenced" else [])
            assert extra == ({"recorded_decision": {"fixture": True}, "writer_kind": "agent"}
                             if kind == "recorded" else {})
            calls.append("comment")
            return 71

        monkeypatch.setattr(kb, "add_comment", add)
        comments = {
            "none": None,
            "tuple": ("fixture-author", "fixture-body"),
            "recorded": RecordedDecisionComment("fixture-author", "fixture-body", {"fixture": True}),
            "fenced": FencedReasonComment("fixture-author", "fixture-body", before),
        }
        with reason_comment(conn, "fixture", comments[kind]) as state:
            assert state.comment_id == (None if kind == "none" else 71)
            state.keep()
        assert calls == ([] if kind == "none" else
                         ["fence", "comment"] if kind == "fenced" else ["comment"])


def test_refusing_fence_prevents_comment_and_context_entry(monkeypatch):
    calls = []
    with closing(sqlite3.connect(":memory:")) as conn:
        def refuse(actual):
            assert actual is conn
            raise ValueError("fixture fence refusal")

        def add(*args, **kwargs):
            calls.append("comment")
            pytest.fail("comment was inserted before refusing fence")

        monkeypatch.setattr(kb, "add_comment", add)
        comment = FencedReasonComment("fixture-author", "fixture-body", refuse)
        with pytest.raises(ValueError, match="fixture fence refusal"):
            with reason_comment(conn, "fixture", comment):
                pytest.fail("context entered after refusal")
        assert calls == []
