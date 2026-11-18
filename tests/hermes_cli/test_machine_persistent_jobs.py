"""Persistent-work safety observations must fail closed across Board downgrades."""
from hermes_cli import machine_drain_metrics as drain
from hermes_cli.kanban_sync_client import HubError


def test_persistent_pair_rejection_preserves_counters_without_replaying_safety():
    calls, rejected = [], []
    sample = {"active_card_runs": 0, "persistent_jobs": 0, "persistent_jobs_at_ms": 1000,
              "tokens_io_total": 42, "tokens_by_model": {"model": [42, 0]},
              "cpu_busy_pct": 1, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1000}

    def send(payload):
        calls.append(dict(payload))
        if "persistent_jobs" in payload:
            raise HubError(400, "unknown field(s): persistent_jobs, persistent_jobs_at_ms")
        return "ok"

    assert drain.send_compatible(send, sample, lambda: rejected.append(True)) == "ok"
    assert len(calls) == 2
    assert calls[-1] == {"active_card_runs": 0, "tokens_io_total": 42,
                         "tokens_by_model": {"model": [42, 0]}}
    assert rejected == []
    assert sample["persistent_jobs"] == 0
