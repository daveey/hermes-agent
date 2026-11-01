"""Upstream display redaction composes with fork early-answer delivery."""
import json

from tools import clarify_early
from tools.clarify_tool import clarify_tool


def test_redacted_display_and_raw_choice_mapping_with_early_provider(monkeypatch):
    import agent.redact

    monkeypatch.setattr(agent.redact, "redact_approval_display", lambda text, **kw: text.replace("private", "***"))
    dispose = clarify_early.register_provider(lambda sid, tcid: None)
    shown = []

    def callback(questions):
        shown.extend(questions)
        return {"answers": {"q0": questions[0]["choices"][0]}, "outcome": "submitted"}

    try:
        result = json.loads(clarify_tool(
            [{"question": "Use private?", "choices": ["private", "public"]}],
            callback=callback, session_id="s-redaction", tool_call_id="call-redaction"))
        assert shown[0]["question"] == "Use ***?"
        assert shown[0]["choices_offered"] == ["***", "public"]
        assert result["responses"][0]["user_response"] == "private"
    finally:
        dispose()
