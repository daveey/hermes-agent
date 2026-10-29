"""A blocked card's ``human_request`` may carry decision ``choices`` (2-6 short {label, value}); they survive
validation and storage on create and block, and a malformed list is refused. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as db
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_human_request import validate_human_request

CHOICES = [{"label": "Ship it", "value": "ship"}, {"label": "Hold", "value": "hold"}]
REQUEST = dict(action="Reply ship or hold in this chat. Unblock = deploy; archive = drop it.",
               why_human="Release owner decision required", interaction="approval", scope="Staging only",
               evidence="Release policy", choices=CHOICES)


def _stored(tid):
    with connect_closing() as conn:
        return json.loads(conn.execute("SELECT human_request FROM tasks WHERE id = ?", (tid,)).fetchone()[0])


def test_choices_are_kept_on_create_and_on_block():
    with connect_closing() as conn:
        tid = db.create_task(conn, title="Deploy?", assignee="owner", initial_status="blocked",
                             human_request=REQUEST, lint_blocked=False)
    assert _stored(tid)["choices"] == CHOICES
    with connect_closing() as conn:
        other = db.create_task(conn, title="Deploy later?", assignee="owner")
        assert db.block_task(conn, other, reason="owner decides", kind="needs_input", human_request=REQUEST)
    assert _stored(other)["choices"] == CHOICES


@pytest.mark.parametrize("bad", [
    "ship or hold",                                              # not a list
    [{"label": "Only one", "value": "one"}],                     # fewer than 2
    [{"label": f"c{i}", "value": f"v{i}"} for i in range(7)],    # more than 6
    [{"label": "Ship", "value": ""}, {"label": "Hold", "value": "hold"}],     # empty value
    [{"label": "x" * 61, "value": "a"}, {"label": "Hold", "value": "hold"}],  # label too long
    [{"label": "A", "value": "same"}, {"label": "B", "value": "same"}],       # duplicate value
])
def test_invalid_choices_are_rejected(bad):
    with pytest.raises(ValueError, match="choice"):
        validate_human_request({**REQUEST, "choices": bad})
    with connect_closing() as conn, pytest.raises(ValueError, match="choice"):
        db.create_task(conn, title="Bad ask", assignee="owner", initial_status="blocked",
                       human_request={**REQUEST, "choices": bad}, lint_blocked=False)


def test_a_request_without_choices_is_unchanged():
    plain = {k: v for k, v in REQUEST.items() if k != "choices"}
    assert validate_human_request(plain) == plain
