"""User-facing wording for kanban ``gave_up`` / ``crashed`` events: accurate cause, retry state, owner."""

from types import SimpleNamespace

from tui_gateway.server import _format_kanban_event_text

SUB = {"task_id": "t_1418b643"}
TASK = SimpleNamespace(title="Fix notification wording", assignee="worker", result=None)
ANSI_REFUSAL = (
    "pid 4242 exited with code 1 Worker's last output: "
    "'\x1b[31m\u2502 Model declined this request (safety refusal). \x1b[0m'"
)


def _text(kind, payload):
    return _format_kanban_event_text(SUB, TASK, SimpleNamespace(kind=kind, payload=payload), "main")


def _headline(text):
    return text.split("\nDetails:")[0]


def test_gave_up_after_refusal_is_a_refusal_not_a_spawn_failure():
    text = _text("gave_up", {"failures": 3, "effective_limit": 3, "error": ANSI_REFUSAL,
                             "trigger_outcome": "crashed", "retry_status": "ready"})
    head = _headline(text)
    assert "Provider unknown (unknown) refused the content of run unknown" in head
    assert "Not a worker crash; no automatic retry" in head
    assert "rephrase or re-scope the card" in head
    assert "then promote it, or archive it" in head
    assert "alternate provider" not in text and "no user action" not in head
    assert "exited unexpectedly" not in head and "retry policy applies" not in head
    # Legacy errors without a Provider said: delimiter retain their sanitized excerpt.
    assert "\x1b" not in text and "pid 4242" in head


def test_generic_crash_event_names_title_retry_and_owner():
    text = _text("crashed", {"pid": 99, "exit_kind": "nonzero_exit", "exit_code": 137, "retry_status": "ready"})
    head = _headline(text)
    assert "Fix notification wording" in head and "exited unexpectedly" in head
    assert "retry policy applies" in head
    assert "pid gone" not in text and "pid" not in head
    assert "no user action identified" in head
    assert "pid 99" in text.split("Details:")[1]


def test_crash_with_refusal_output_never_promises_retry_even_with_legacy_ready_state():
    text = _text("crashed", {"pid": 7, "worker_output": "Model declined: safety refusal", "retry_status": "ready",
                             "provider": "openai-codex", "model": "fixture-model", "run_number": 203994})
    head = _headline(text)
    assert "Provider openai-codex (fixture-model) refused the content of run 203994" in head
    assert "Not a worker crash; no automatic retry" in head
    assert "retry policy applies" not in head and "no user action identified" not in head


def test_true_spawn_failure_still_says_spawn():
    text = _text("gave_up", {"failures": 2, "effective_limit": 2, "trigger_outcome": "spawn_failed",
                             "error": "spawn failed: [Errno 2] No such file or directory: 'hermes'"})
    head = _headline(text)
    assert "could not be launched" in head and "2 of 2" in head
    assert "refus" not in head
    assert "No such file" in text.split("Details:")[1]


def test_connection_refused_is_not_a_model_refusal():
    text = _text("gave_up", {"failures": 3, "trigger_outcome": "crashed", "error": "connect: Connection refused"})
    assert "refus" not in _headline(text).lower()
    assert "exited unexpectedly" in _headline(text)


def test_terminal_provider_config_is_the_one_actionable_ask():
    text = _text("gave_up", {"failures": 1, "terminal_provider": True, "trigger_outcome": "crashed",
                             "error": "pid 5 exited on a terminal provider error"})
    head = _headline(text)
    assert "provider configuration" in head and "task is Blocked" in head
    assert "credential" not in head  # exit 78 does not record which provider reason fired
    assert "no user action" not in head


def test_human_input_block_is_untouched():
    text = _text("blocked", {"kind": "needs_input", "reason": "Which account should I use?"})
    # Blocks bypass the failure classifier; the shared block presenter keeps the reason verbatim.
    # Without a validated human_request it must not claim the user owns the next step.
    assert "Reason: Which account should I use?" in text
    assert "Hermes needs to recover" not in text
    assert "actor: user" not in text


def test_poller_marks_failure_notices_diagnostic():
    from gateway.kanban_watchers_notifier import diagnostic_event

    for kind in ("gave_up", "crashed"):
        assert diagnostic_event(SimpleNamespace(kind=kind, payload={}))


def test_missing_or_todo_retry_status_makes_no_retry_promise():
    for payload in ({"pid": 1}, {"pid": 1, "retry_status": "todo"}):
        head = _headline(_text("crashed", payload))
        assert "retry state was not recorded" in head and "will retry" not in head
        assert "retry policy applies" not in head


def test_blocked_retry_status_says_blocked():
    assert "task is Blocked" in _headline(_text("crashed", {"retry_status": "blocked"}))


def test_timed_out_promises_retry_only_when_recorded():
    assert "will retry" in _text("timed_out", {"limit_seconds": 60, "retry_status": "ready"})
    for payload in ({"limit_seconds": 60}, {"limit_seconds": 60, "retry_status": "todo"}):
        text = _text("timed_out", payload)
        assert "will retry" not in text and "not recorded" in text
