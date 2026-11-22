"""Queued ownership retry preserves the resume -> history lock order."""
from hermes_cli import active_sessions, admission_fence
from tests.tui_gateway.test_update_hold_submit_queue import _submit
from tests.tui_gateway.test_update_hold_submit_queue import held as held  # noqa: PLC0414 -- pytest fixture re-export
from tui_gateway import server


def test_queue_drain_does_not_take_resume_under_history(held, monkeypatch):
    _submit()
    admission_fence.clear_hold(held.home)
    held.session.pop("active_session_lease", None)
    monkeypatch.setattr(server, "_canonical_ledger_refusal", lambda session: None)
    monkeypatch.setattr(server, "_durable_owner_refusal", lambda session: None)
    refusal = active_sessions.ActiveSessionRefusal("foreign slot", "SESSION_NOT_OWNED")
    refusal.ownership_cause = "lease_foreign"
    refusal.holder_pid = 123
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **kw: (None, refusal))

    class ResumeLock:
        def __enter__(self):
            assert not held.session["history_lock"]._is_owned(), (
                "drain must not acquire resume_lock under history_lock: "
                "redrive_update_queues acquires them in the reverse order"
            )
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(server, "_session_resume_lock", ResumeLock())
    server._drain_queued_prompt("review", "sid", held.session)
