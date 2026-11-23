"""Decision notices are historical prompts, not fresh release authority."""
from __future__ import annotations

import pytest

from hermes_cli.kanban_decision_ask import notice_text


@pytest.mark.parametrize("surface", ["shared", "gateway", "tui"])
@pytest.mark.parametrize("administrative", [False, True])
def test_delayed_decision_notice_requires_reconciliation(surface, administrative):
    payload = {"why": "Select global identity protocol and milestones A-D."}
    if administrative:
        payload["actionability"] = {"kind": "administrative_only"}
    if surface == "gateway":
        from gateway import kanban_watchers_notifier as gn
        from types import SimpleNamespace
        _, text, _ = gn._EVENT_FORMATTERS["decision_requested"](
            SimpleNamespace(payload=payload), SimpleNamespace(head="fixture"))
    elif surface == "tui":
        from tui_gateway import session_notifications as sn
        _, render = sn._KANBAN_EVENT_FORMATTERS["decision_requested"]
        text = render(None, payload, "t")
    else:
        text = notice_text(payload)
    assert payload["why"] in text
    assert "current status and newer owner decisions" in text
    assert "already answered or superseded" in text
    assert "do not post another waking comment" in text
    assert "not product progress" in text
    assert "This card is parked until you decide" not in text
    if administrative:
        assert "never release or rerun the root scope" in text
    else:
        assert "only if the decision is still outstanding" in text
        assert "kanban_comment" in text
        assert "concrete human-only" in text


def test_notice_reconciliation_keeps_question_fallback_and_bounds():
    text = notice_text({"reason": "Preserve stopped scope; no retry."})
    assert "Preserve stopped scope; no retry." in text
    long = notice_text({"why": "A" * 7000 + "END"})
    assert "full text in the card comments" in long
    assert "END" in long
    assert "current status and newer owner decisions" in long
