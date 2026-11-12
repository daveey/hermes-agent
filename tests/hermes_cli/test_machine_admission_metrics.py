"""Opted-in hub policy and its pool census travel as one fail-closed tuple."""
import pytest

from hermes_cli import machine_metrics as mm
from hermes_cli import config_backend


POLICY = {"kanban": {"max_in_progress": 7, "admission": {
    "cpu_busy_below_pct": 50, "priority_slots": 3, "priority_cpu_busy_below_pct": 85,
}}}
EXPECTED = {"admission_version": 1, "admission_regular_slots": 7, "admission_priority_slots": 3,
            "admission_regular_cpu_limit": 50, "admission_priority_cpu_limit": 85,
            "active_regular_runs": 7, "active_priority_runs": 1}


def test_collect_uses_one_config_and_one_census(monkeypatch, tmp_path):
    reads, counts = [], []

    def read(path):
        reads.append(path)
        return POLICY

    def census(*, detail=None):
        counts.append(detail)
        if detail is not None:
            detail["pools"].update(regular=7, priority=1)
        return 8

    monkeypatch.setattr(config_backend, "read_config_doc_readonly", read)
    monkeypatch.setattr(mm, "active_card_runs", census)
    out = mm.collect(tmp_path)
    assert {key: out.get(key) for key in EXPECTED} == EXPECTED
    assert out["active_card_runs"] == 8
    assert len(reads) == len(counts) == 1


def test_old_board_strips_admission_tuple_and_cpu_on_single_retry(monkeypatch):
    from hermes_cli.kanban_sync_client import HubError
    from hermes_cli import machine_admission_metrics as admission

    monkeypatch.setattr(admission, "policy", lambda: {k: v for k, v in EXPECTED.items() if k.startswith("admission_")})
    calls = []

    def send(sample):
        calls.append(sample)
        if any(key in sample for key in EXPECTED):
            raise HubError(400, "unknown metric")

    reporter = mm.Reporter(send, collect_fn=lambda: {"cpus": 4, "active_card_runs": 8, **EXPECTED},
                           cpu_limit_fn=lambda: ("off", None))
    assert reporter.report_due()
    assert calls[1] == {"cpus": 4, "active_card_runs": 8}
    assert len(calls) == 2


@pytest.mark.parametrize("changed", ["off", "changed", "same"])
def test_reporter_rechecks_policy_after_cpu_sample(monkeypatch, changed):
    from hermes_cli import machine_admission_metrics as admission

    settings = {k: v for k, v in EXPECTED.items() if k.startswith("admission_")}
    current = {} if changed == "off" else {**settings, "admission_priority_slots": 4} if changed == "changed" else settings
    monkeypatch.setattr(admission, "policy", lambda: current)
    monkeypatch.setattr(mm, "record_hub_cpu_sample", lambda sample: None)
    sent = []
    reporter = mm.Reporter(sent.append, collect_fn=lambda: {"active_card_runs": 8, **EXPECTED},
                           cpu_limit_fn=lambda: ("on", 50),
                           cpu_sample_fn=lambda: {"cpu_busy_pct": 10, "cpu_busy_window_ms": 1000,
                                                  "cpu_busy_sample_end_ms": 100000})
    assert reporter.report_due()
    assert ("admission_version" in sent[0]) == (changed == "same")
    assert sent[0]["active_card_runs"] == 8


@pytest.mark.parametrize("doc", [None, [], {}, {"kanban": []}, {"kanban": {"admission": []}},
    *({"kanban": {"max_in_progress": cap, "admission": POLICY["kanban"]["admission"]}} for cap in [True, -1, 0, 257]),
    *({"kanban": {"max_in_progress": 7, "admission": {**POLICY["kanban"]["admission"], key: value}}}
      for key, value in [("cpu_busy_below_pct", None), ("priority_slots", 0), ("priority_slots", True),
                         ("priority_slots", 257), ("priority_cpu_busy_below_pct", 101)])])
def test_invalid_or_disabled_policy_does_not_advertise_support(monkeypatch, tmp_path, doc):
    monkeypatch.setattr(config_backend, "read_config_doc_readonly", lambda path: doc)
    calls = []

    def count():
        calls.append(1)
        return 8

    monkeypatch.setattr(mm, "active_card_runs", count)
    out = mm.collect(tmp_path)
    assert not set(EXPECTED).intersection(out)
    assert out["active_card_runs"] == 8 and len(calls) == 1


@pytest.mark.parametrize("active,regular,priority", [(None, 7, 1), (9, 7, 1), (8, -1, 9), (8, True, 7)])
def test_bad_census_omits_entire_tuple(monkeypatch, tmp_path, active, regular, priority):
    monkeypatch.setattr(config_backend, "read_config_doc_readonly", lambda path: POLICY)

    def count(*, detail):
        detail["pools"].update(regular=regular, priority=priority)
        return active

    monkeypatch.setattr(mm, "active_card_runs", count)
    out = mm.collect(tmp_path)
    assert not set(EXPECTED).intersection(out)
    assert out["active_card_runs"] == active
