"""Source-owned validity; no consumer read turns history into live execution."""
from copy import deepcopy

import pytest

from hermes_cli.execution_observations import adapt, capacity_reason


def observation():
    return {
        "version": 1,
        "identity": {"gateway": "gateway-a", "profile": "default", "board": "sample",
                     "kind": "card", "work_id": "task-a", "run_id": 7},
        "owner": {"gateway": "gateway-a", "profile": "default"},
        "state": "queued", "source": "dispatcher", "source_revision": "event:31",
        "evidence_refs": ["event:31"], "observed_at": 1000, "valid_until": 1180,
        "last_activity": 1000, "last_progress": None, "diagnostics": [],
    }


def test_current_preserves_source_and_separates_progress():
    raw = observation()
    before = deepcopy(raw)
    out = adapt(raw, now=1100, source_class="sidebar")
    assert out["state"] == "queued" and out["freshness"] == "current"
    assert out["observed_at"] == 1000 and out["valid_until"] == 1180
    assert out["age_seconds"] == 100
    assert out["last_progress"] is None
    assert raw == before


@pytest.mark.parametrize("now,expected", [(1180, "stale"), (1200, "stale"), (999, "unknown")])
def test_stale_and_clock_skew_never_mean_idle_or_failed(now, expected):
    out = adapt(observation(), now=now, source_class="sidebar")
    assert out["state"] == "unknown"
    assert out["last_known_state"] == "queued"
    assert out["freshness"] == expected
    assert out["observed_at"] == 1000


@pytest.mark.parametrize("field", ["identity", "source", "source_revision", "observed_at", "valid_until"])
def test_missing_required_source_is_unknown_without_fabrication(field):
    raw = observation()
    del raw[field]
    out = adapt(raw, now=1100, source_class="sidebar")
    assert out["state"] == "unknown" and out["unknown_reason"]
    assert out["observed_at"] == raw.get("observed_at")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "1000", -1])
def test_invalid_clock_is_unknown(value):
    raw = observation()
    raw["observed_at"] = value
    assert adapt(raw, now=1100, source_class="sidebar")["state"] == "unknown"


def test_board_and_attempt_identity_required_for_card():
    for key in ("board", "run_id"):
        raw = observation()
        del raw["identity"][key]
        out = adapt(raw, now=1100, source_class="sidebar")
        assert out["state"] == "unknown"
        assert key not in out["identity"]


def test_generation_is_valid_without_invented_run():
    raw = observation()
    del raw["identity"]["run_id"]
    raw["identity"]["generation"] = "ready-event:4"
    out = adapt(raw, now=1100, source_class="sidebar")
    assert out["state"] == "queued" and "run_id" not in out["identity"]


def test_source_validity_is_minimum_and_upper_bounds_are_separate():
    raw = observation()
    raw["valid_until"] = 5000
    assert adapt(raw, now=1200, source_class="sidebar")["state"] == "unknown"
    imported = adapt(raw, now=1200, source_class="imported_owner")
    assert imported["valid_until"] == 1600 and imported["state"] == "queued"
    raw["required_sources"] = [{"observed_at": 990, "valid_until": 1050}]
    assert adapt(raw, now=1100, source_class="imported_owner")["state"] == "unknown"


def test_fetch_never_renews_ttl():
    first = adapt(observation(), now=1100, source_class="sidebar")
    second = adapt(first, now=1200, source_class="sidebar")
    assert second["state"] == "unknown" and second["valid_until"] == 1180
    assert second["last_known_state"] == "queued"


@pytest.mark.parametrize('missing_identity', [False, True])
def test_nested_reason_expires_independently_on_cached_read(missing_identity):
    raw = observation()
    if missing_identity:
        del raw['identity']['run_id']
    raw['non_running_reason'] = {
        'code': 'usage_accounting_pending', 'family': 'accounting', 'actor': 'agent',
        'disposition': 'blocked', 'summary': 'Run usage accounting is unsettled',
        'next_action': 'Recover the original source accounting evidence, then retry normal admission',
        'freshness': 'current', 'observed_at': 990, 'valid_until': 1010,
        'evidence_ref': 'dispatch:original', 'source_revision': 'original',
    }
    before = deepcopy(raw)
    first = adapt(raw, now=1005, source_class='sidebar')
    assert first['non_running_reason']['freshness'] == 'current'
    stale = adapt(first, now=1010, source_class='sidebar')['non_running_reason']
    assert stale['freshness'] == 'unknown'
    assert stale['disposition'] == 'unknown'
    assert stale['observed_at'] == 990
    assert stale['unknown_reason'] == 'dispatcher_evidence_stale'
    assert raw == before


def test_legacy_unknown_retains_human_request_and_delivery_receipt():
    legacy = {"state": "running", "human_request": {"action": "answer"},
              "completion_receipt": {"verified": True}}
    out = adapt(legacy, now=1100, source_class="sidebar")
    assert out["state"] == "unknown" and out["observed_at"] is None
    assert out["human_request"] == legacy["human_request"]
    assert out["completion_receipt"] == legacy["completion_receipt"]


@pytest.mark.parametrize("code", ["memory_pressure", "provider_limit", "placement", "dependency", "update_drain"])
def test_namespaced_concurrent_native_diagnostics(code):
    reason = capacity_reason(code, native_code="native_hold", phase="pre_claim", evidence_ref="event:2")
    assert reason == {"code": "capacity." + code, "native_code": "native_hold",
                      "phase": "pre_claim", "evidence_ref": "event:2"}
    raw = observation()
    raw["diagnostics"] = [reason, capacity_reason("placement", native_code="missing_labels",
                                                 phase="pre_claim", evidence_ref="event:3")]
    assert adapt(raw, now=1100, source_class="sidebar")["diagnostics"] == raw["diagnostics"]


def test_provider_after_start_stays_after_start():
    reason = capacity_reason("provider_limit", native_code="rate_limited", phase="post_start", evidence_ref="event:8")
    assert reason["phase"] == "post_start"


@pytest.mark.parametrize("field,value", [
    ("owner", None), ("owner", {}), ("owner", {"gateway": "g"}),
    ("owner", {"gateway": "g", "profile": False}),
    ("evidence_refs", None), ("evidence_refs", []), ("evidence_refs", [""]),
    ("evidence_refs", "event:31"), ("evidence_refs", [True]),
])
def test_missing_owner_or_evidence_is_unknown(field, value):
    raw = observation()
    raw[field] = value
    out = adapt(raw, now=1100, source_class="sidebar")
    assert out["state"] == "unknown"
    assert out["last_known_state"] == "queued"


@pytest.mark.parametrize("attempt", [-1, 0, True, "", [], {}])
def test_invalid_attempt_identity_is_unknown(attempt):
    raw = observation()
    raw["identity"]["run_id"] = attempt
    assert adapt(raw, now=1100, source_class="sidebar")["state"] == "unknown"


def test_required_source_retains_its_own_shorter_ttl():
    raw = observation()
    raw["valid_until"] = 1600
    raw["required_sources"] = [{"observed_at": 990, "valid_until": 5000,
                                "source_class": "sidebar"}]
    out = adapt(raw, now=1200, source_class="imported_owner")
    assert out["state"] == "unknown" and out["valid_until"] == 1170


def test_unknown_required_source_class_is_unknown():
    raw = observation()
    raw["required_sources"] = [{"observed_at": 990, "valid_until": 1180,
                                "source_class": "invented"}]
    assert adapt(raw, now=1100, source_class="sidebar")["state"] == "unknown"


def test_unknown_source_class_cannot_manufacture_validity():
    assert adapt(observation(), now=1100, source_class="invented")["state"] == "unknown"
