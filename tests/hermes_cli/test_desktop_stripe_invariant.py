"""``stripe-invariant-violation <sid>``: a non-red stripe reported while the row has an open human ask."""

from __future__ import annotations

import logging

import pytest

from hermes_cli import desktop_stripe_report as report


@pytest.mark.parametrize("stripe,reasons,violated", [
    ("done", ["needs_you:2"], True),
    ("waiting", ["needs_you:1"], True),
    ("periodic", ["needs_you:1"], True),
    ("none", ["needs_you:3"], True),
    (None, ["needs_you:1"], True),
    ("stuck", ["needs_you:2", "kanban-blocked:2"], False),
    ("blocked", ["needs_you:1"], False),  # a deliberate ⌘B park
    ("done", [], False),
    ("done", ["something-else"], False),
])
def test_invariant_violated(stripe, reasons, violated):
    assert report.invariant_violated(stripe, reasons) is violated


def test_record_logs_the_violation_for_the_session(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=report.logger.name):
        report.record(tmp_path, "sess-1", "done", ["needs_you:2"], previous="stuck")
        report.record(tmp_path, "sess-2", "stuck", ["kanban-blocked:2"], previous="done")
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1 and lines[0].startswith("stripe-invariant-violation sess-1")
