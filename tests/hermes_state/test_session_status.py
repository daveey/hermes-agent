"""Canonical status is work evidence, never prose or UI-fetch state."""
from copy import deepcopy
import importlib.util

import pytest


IDENTITY = {"gateway": "gateway-a", "profile": "default", "session_id": "chat", "owner_epoch": "owner-1"}
REQUIRED = ("cards", "controls", "turn", "subagents", "processes", "goals")


def snapshot(**changes):
    value = {
        "identity": dict(IDENTITY), "generation": 7, "started": True,
        "turn": {"state": "succeeded", "generation": 7},
        "required_sources": list(REQUIRED),
        "sources": {name: {"identity": dict(IDENTITY), "generation": 7,
                           "revision": 3, "current_revision": 3, "coverage": "complete"}
                    for name in REQUIRED},
        "work": [], "asks": [], "muted": False,
    }
    value.update(changes)
    return value


def reduce(value, previous=None):
    # A missing reducer is an assertion failure at RED, not a collection/import error.
    spec = importlib.util.find_spec("hermes_state_session_status")
    assert spec is not None, "canonical backend reducer has not been implemented"
    return importlib.import_module("hermes_state_session_status").reduce_status(value, previous=previous)


@pytest.mark.parametrize("changes,kind,stripe", [
    ({}, "done", "done"),
    ({"muted": True, "asks": [{"id": "ask"}]}, "muted", "blocked"),
    ({"asks": [{"id": "ask"}], "turn": {"state": "running", "generation": 7}}, "decision", "stuck"),
    ({"turn": {"state": "running", "generation": 7}}, "running", "waiting"),
    ({"work": [{"id": "card", "state": "ready"}]}, "waiting", "waiting"),
    ({"work": [{"id": "card", "kind": "card", "state": "external_unknown"}]}, "waiting", "external"),
    ({"work": [{"id": "card", "state": "external_unknown"}], "asks": [{"id": "ask"}]}, "decision", "stuck"),
    ({"work": [{"id": "card", "state": "external_unknown"}], "muted": True}, "muted", "blocked"),
    ({"work": [{"id": "card", "state": "external_unknown"}],
      "turn": {"state": "running", "generation": 7}}, "running", "waiting"),
    ({"work": [{"id": "ci", "state": "external_unknown"},
               {"id": "running", "kind": "card", "state": "running"}]}, "running", "waiting"),
    ({"work": [{"id": "child", "state": "running"}]}, "waiting", "waiting"),
    ({"work": [{"id": "child", "state": "failed"}]}, "unresolved", "attention"),
    ({"work": [{"id": "child", "state": "lost"}]}, "unresolved", "attention"),
    ({"work": [{"id": "child", "state": "cancelled"}]}, "unresolved", "attention"),
    ({"work": [{"id": "child", "state": "invented-success"}]}, "unresolved", "attention"),
    ({"work": [{"id": "child", "state": "succeeded"}]}, "done", "done"),
    ({"work": [{"id": "watch", "state": "watching", "passive": True}]}, "done", "done"),
    ({"work": [{"id": "cron", "state": "scheduled", "passive": True}]}, "done", "done"),
    ({"started": False, "turn": {"state": "never_started", "generation": 7}}, "unresolved", "none"),
    ({"turn": {"state": "failed", "generation": 7}}, "unresolved", "attention"),
    ({"turn": {"state": "pending", "generation": 7}}, "unresolved", "attention"),
    # Overdue triage is operational red, never above a user mute or a human ask.
    ({"work": [{"id": "t1", "kind": "card", "state": "triage_overdue"}]}, "waiting", "stuck"),
    ({"work": [{"id": "t1", "kind": "card", "state": "triage_overdue"}], "muted": True}, "muted", "blocked"),
    ({"work": [{"id": "t1", "kind": "card", "state": "triage_overdue"}], "muted": True,
      "asks": [{"id": "ask"}]}, "muted", "blocked"),
    ({"work": [{"id": "t1", "kind": "card", "state": "triage_overdue"}], "asks": [{"id": "ask"}]},
     "decision", "stuck"),
    # A stopped, retained scope stays unresolved (never a wait); only its age turns the stripe red.
    ({"work": [{"id": "t1", "kind": "card", "state": "retained"}]}, "unresolved", "attention"),
    ({"work": [{"id": "t1", "kind": "card", "state": "retained", "triage_overdue": True}]}, "unresolved", "stuck"),
    ({"work": [{"id": "t1", "kind": "card", "state": "retained", "triage_overdue": 1}]}, "unresolved", "attention"),
    ({"work": [{"id": "t1", "kind": "card", "state": "retained", "triage_overdue": True}], "muted": True},
     "muted", "blocked"),
    ({"work": [{"id": "t1", "kind": "process", "state": "retained", "triage_overdue": True}]}, "unresolved", "attention"),
])
def test_precedence(changes, kind, stripe):
    out = reduce(snapshot(**changes))
    assert (out["kind"], out["presentation"]["stripe"]) == (kind, stripe)
    assert out["status_version"] == 1
    assert out["identity"] == IDENTITY
    assert out["generation"] == 7
    assert out["revision"] == 1


def test_muted_overdue_triage_keeps_mute_presentation():
    out = reduce(snapshot(muted=True, work=[{"id": "t1", "kind": "card", "state": "triage_overdue"}]))
    assert out["presentation"] == {"stripe": "blocked", "reason_codes": ["muted"]}
    assert "on" not in out or all(item.get("state") != "triage_overdue" for item in out["on"])


@pytest.mark.parametrize("defect", ["missing", "unknown", "epoch", "generation", "inconsistent", "expired"])
@pytest.mark.parametrize("source", REQUIRED)
def test_incomplete_or_foreign_source_never_green(source, defect):
    value = snapshot()
    reading = value["sources"][source]
    defects = {
        "missing": lambda: value["sources"].pop(source),
        "unknown": lambda: reading.update(coverage="unknown"),
        "epoch": lambda: reading["identity"].update(owner_epoch="previous-owner"),
        "generation": lambda: reading.update(generation=6),
        "inconsistent": lambda: reading.update(current_revision=4),
        "expired": lambda: reading.update(expired=True),
    }
    defects[defect]()
    out = reduce(value)
    assert out["kind"] == "unresolved"
    assert out["presentation"]["stripe"] != "done"
    assert out["coverage"]["state"] == "unknown"
    assert any(source in reason for reason in out["coverage"]["reasons"])


def test_running_card_publishes_running_and_preserves_all_open_work():
    work = [{"id": "card", "kind": "card", "state": "running"},
            {"id": "queued", "kind": "card", "state": "todo"}]
    out = reduce(snapshot(work=work))
    assert out["kind"] == "running"
    assert out["presentation"] == {"stripe": "waiting", "reason_codes": ["linked_work_running"]}
    assert out["on"] == work
    work[0]["state"] = "done"
    assert out["on"][0]["state"] == "running"


@pytest.mark.parametrize("state", ["todo", "scheduled"])
def test_nonrunning_cards_remain_waiting(state):
    out = reduce(snapshot(work=[{"id": "card", "kind": "card", "state": state}]))
    assert out["kind"] == "waiting"
    assert out["presentation"]["reason_codes"] == ["linked_work_open"]


@pytest.mark.parametrize("changes,kind,reason", [
    ({"asks": [{"id": "ask"}]}, "decision", "human_ask"),
    ({"muted": True}, "muted", "muted"),
    ({"turn": {"state": "running", "generation": 7}}, "running", "own_turn_active"),
])
def test_running_card_preserves_higher_precedence(changes, kind, reason):
    out = reduce(snapshot(work=[{"id": "card", "kind": "card", "state": "running"}], **changes))
    assert out["kind"] == kind
    assert out["presentation"]["reason_codes"] == [reason]


@pytest.mark.parametrize("kind", ["process", "goal", "subagent"])
def test_other_running_work_remains_waiting(kind):
    out = reduce(snapshot(work=[{"id": "other", "kind": kind, "state": "running"}]))
    assert out["kind"] == "waiting"
    assert out["presentation"]["reason_codes"] == ["linked_work_open"]


def test_revision_only_changes_when_canonical_evidence_changes():
    value = snapshot()
    first = reduce(value)
    assert reduce(value, first) == first
    changed = deepcopy(value)
    changed["sources"]["cards"]["revision"] = 4
    changed["sources"]["cards"]["current_revision"] = 4
    second = reduce(changed, first)
    assert second["revision"] == 2
    assert second["source_revisions"]["cards"] == 4


@pytest.mark.parametrize("wording", ["Done.", "I will do it", "Are you sure?", "", "Everything failed."])
def test_wording_and_declaration_are_not_status_authority(wording):
    value = snapshot(summary=wording, declared={"kind": "decision", "summary": wording})
    assert reduce(value)["kind"] == "done"
    value["turn"]["state"] = "failed"
    value["declared"]["kind"] = "done"
    assert reduce(value)["kind"] == "unresolved"


def test_new_generation_cannot_inherit_success_or_erase_failure_without_successor():
    first = reduce(snapshot(turn={"state": "failed", "generation": 7}))
    pending = snapshot(generation=8, turn={"state": "pending", "generation": 8})
    for reading in pending["sources"].values():
        reading["generation"] = 8
    assert reduce(pending, first)["kind"] == "unresolved"
    pending["turn"] = {"state": "succeeded", "generation": 7}
    assert reduce(pending, first)["kind"] == "unresolved"
    pending["turn"]["generation"] = 8
    assert reduce(pending, first)["kind"] == "done"


def test_late_generation_is_rejected_without_overwriting_current_status():
    current = reduce(snapshot(generation=8, turn={"state": "running", "generation": 8}))
    with pytest.raises(ValueError, match="stale generation"):
        reduce(snapshot(), current)


def test_owner_change_requires_a_new_qualified_snapshot():
    current = reduce(snapshot())
    value = snapshot()
    value["identity"]["owner_epoch"] = "owner-2"
    out = reduce(value, current)
    assert out["kind"] == "unresolved"
    assert out["revision"] == current["revision"] + 1


def test_known_ask_and_own_running_precede_unknown_coverage():
    value = snapshot(sources={}, asks=[{"id": "ask"}])
    assert reduce(value)["kind"] == "decision"
    value["asks"] = []
    value["turn"] = {"state": "running", "generation": 7}
    assert reduce(value)["kind"] == "running"
    assert reduce(value)["coverage"]["state"] == "unknown"


@pytest.mark.parametrize("state", ["failed", "lost", "cancelled"])
def test_disappearing_failed_work_does_not_become_empty_success(state):
    failed = reduce(snapshot(work=[{"id": "child-1", "state": state}]))
    clean = snapshot()
    again = reduce(clean, failed)
    assert again["kind"] == "unresolved"
    assert again["failures"] == failed["failures"]
    clean["generation"] = 8
    clean["turn"] = {"state": "pending", "generation": 8}
    for reading in clean["sources"].values():
        reading["generation"] = 8
    pending_out = reduce(clean, failed)
    assert pending_out["kind"] == "unresolved"
    clean["turn"]["state"] = "succeeded"
    assert reduce(clean, pending_out)["kind"] == "done"


def test_same_generation_success_does_not_erase_failed_turn():
    failed = reduce(snapshot(turn={"state": "failed", "generation": 7}))
    assert reduce(snapshot(), failed)["kind"] == "unresolved"


def test_unknown_work_state_is_not_latched_as_an_execution_failure():
    unknown = reduce(snapshot(work=[{"id": "child-1", "state": "unknown"}]))
    assert unknown["kind"] == "unresolved"
    assert reduce(snapshot(work=[{"id": "child-1", "state": "succeeded"}]), unknown)["kind"] == "done"


@pytest.mark.parametrize("field", ["gateway", "profile", "session_id", "owner_epoch"])
def test_unqualified_identity_refused(field):
    value = snapshot()
    del value["identity"][field]
    with pytest.raises(ValueError, match="qualified identity"):
        reduce(value)


@pytest.mark.parametrize("generation", [True, -1, "7", None])
def test_invalid_generation_refused(generation):
    with pytest.raises(ValueError, match="generation"):
        reduce(snapshot(generation=generation))


def test_omitting_required_source_list_cannot_certify_empty_reads():
    assert reduce(snapshot(required_sources=[], sources={}))["kind"] == "unresolved"
