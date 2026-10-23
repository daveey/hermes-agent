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
    assert "Fix notification wording" in head
    assert "refused" in head and "refusal" in head
    assert "spawn" not in head.lower()
    assert "3 of 3" in head and "Blocked" in head
    assert "Hermes needs to recover; no user action identified." in head
    assert "no alternate provider is tried automatically" in head
    # raw material lives only under Details, with ANSI stripped
    assert "pid" not in head and "\x1b" not in text and "pid 4242" in text.split("Details:")[1]


def test_generic_crash_event_names_title_retry_and_owner():
    text = _text("crashed", {"pid": 99, "exit_kind": "nonzero_exit", "exit_code": 137, "retry_status": "ready"})
    head = _headline(text)
    assert "Fix notification wording" in head and "exited unexpectedly" in head
    assert "dispatcher will retry automatically" in head
    assert "pid gone" not in text and "pid" not in head
    assert "no user action identified" in head
    assert "pid 99" in text.split("Details:")[1]


def test_crash_with_refusal_output_is_classified_as_refusal_and_retry_stated():
    text = _text("crashed", {"pid": 7, "worker_output": "Model declined: safety refusal", "retry_status": "ready"})
    assert "refused" in _headline(text) and "will retry" in _headline(text)


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
    assert "credential or model" in head and "Fix the provider credential/model" in head
    assert "no user action" not in head


def test_human_input_block_is_untouched():
    text = _text("blocked", {"kind": "needs_input", "reason": "Which account should I use?"})
    assert text.endswith("blocked: Which account should I use?")
    assert "Hermes needs to recover" not in text


def test_poller_marks_failure_notices_diagnostic():
    from gateway.kanban_watchers_notifier import diagnostic_event

    for kind in ("gave_up", "crashed"):
        assert diagnostic_event(SimpleNamespace(kind=kind, payload={}))
