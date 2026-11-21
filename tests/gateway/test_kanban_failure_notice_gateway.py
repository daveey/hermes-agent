"""Gateway (messaging) gave_up/crashed/timed_out notices share the classifier and make no unsupported promises."""

from types import SimpleNamespace

from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

REFUSAL = "Model declined this request (safety refusal)."


def _note(kind, payload):
    n = SimpleNamespace(head="[Kanban t_x]", title="Do the thing", task_id="t_x")
    return _EVENT_FORMATTERS[kind](SimpleNamespace(kind=kind, payload=payload), n)[0]


def test_gave_up_refusal_is_named_and_not_a_generic_failure():
    text = _note("gave_up", {"failures": 3, "effective_limit": 3, "error": REFUSAL,
                             "trigger_outcome": "crashed", "retry_status": "ready"})
    assert "refused the content of run" in text and "no automatic retry" in text
    assert "then promote it, or archive it" in text and "alternate provider" not in text
    assert "hermes kanban log t_x" in text


def test_crashed_without_retry_status_makes_no_retry_promise():
    text = _note("crashed", {"pid": 5, "exit_code": 1})
    assert "retry state was not recorded" in text
    assert "retried automatically" not in text and "will retry" not in text


def test_crashed_with_ready_retry_status_defers_to_policy():
    text = _note("crashed", {"pid": 5, "retry_status": "ready"})
    assert "retry policy applies" in text


def test_connection_refused_crash_is_not_a_model_refusal():
    text = _note("gave_up", {"failures": 3, "error": "connect: Connection refused"})
    assert "model refused" not in text


def test_timed_out_retry_claim_only_when_recorded():
    assert "retried automatically" in _note("timed_out", {"limit_seconds": 600, "retry_status": "ready"})
    text = _note("timed_out", {"limit_seconds": 600})
    assert "retried automatically" not in text and "not recorded" in text
