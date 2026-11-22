"""``stripe-invariant-violation <sid>``: a non-red stripe reported while the row has an open human ask."""

from __future__ import annotations

import logging

import pytest

from hermes_cli import desktop_stripe_report as report


@pytest.mark.parametrize("stripe,reasons,violated", [
    ("done", ["needs_you:2"], True),
    ("waiting", ["needs_you:1"], True),
    ("periodic", ["needs_you:1"], True),
    ("none", ["needs_you:3"], True),
    (None, ["needs_you:1"], True),
    ("stuck", ["needs_you:2", "kanban-blocked:2"], False),
    ("blocked", ["needs_you:1"], False),  # a deliberate ⌘B park
    ("done", [], False),
    ("done", ["something-else"], False),
])
def test_invariant_violated(stripe, reasons, violated):
    assert report.invariant_violated(stripe, reasons) is violated


def test_record_logs_the_violation_for_the_session(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=report.logger.name):
        report.record(tmp_path, "sess-1", "done", ["needs_you:2"], previous="stuck")
        report.record(tmp_path, "sess-2", "stuck", ["kanban-blocked:2"], previous="done")
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1 and lines[0].startswith("stripe-invariant-violation sess-1")


@pytest.fixture
def canonical():
    return {"status_version": 1, "identity": {"gateway": "g", "profile": "default", "session_id": "s", "owner_epoch": "e"},
            "revision": 4, "generation": 2, "coverage": {"state": "complete", "reasons": []},
            "presentation": {"stripe": "done", "reason_codes": []}, "kind": "done"}


def paint(canonical, **changes):
    return {"status_version": 1, "status": "canonical", "identity": canonical["identity"],
            "revision": 4, "generation": 2, "coverage": canonical["coverage"], "stripe": "done",
            "surface": "row", "mounted": True, "observation_client": "client-a", "observation_lifetime": 1, "observation_order": 1, "reason_codes": [], "previous": None, "reasons": [], **changes}


@pytest.mark.parametrize("field,value", [
    ("status_version", True), ("revision", -1), ("generation", True),
    ("generation", -1), ("identity", {"gateway": "", "profile": "default", "session_id": "s", "owner_epoch": "e"}),
])
def test_malformed_matching_envelopes_are_not_agreement(canonical, field, value):
    canonical[field] = value
    observation = paint(canonical)
    observation[field] = value
    assert report.compare(canonical, observation)["drift"] is None


@pytest.mark.parametrize("stripe,drift", [("done", False), ("waiting", True), ("none", True)])
def test_revision_bound_paint(canonical, stripe, drift):
    result = report.compare(canonical, paint(canonical, stripe=stripe))
    assert result["drift"] is drift


@pytest.mark.parametrize("changes,state", [
    ({"revision": 3}, "stale"), ({"revision": 5}, "unknown"),
    ({"identity": {"gateway": "other", "profile": "default", "session_id": "s", "owner_epoch": "e"}}, "refused"),
    ({"status": "unknown"}, "unknown"), ({"status_version": 2}, "unknown"),
    ({"surface": "unmounted"}, "unknown"), ({"revision": None}, "unknown"),
])
def test_nonobservations_never_agree_or_drift(canonical, changes, state):
    result = report.compare(canonical, paint(canonical, **changes))
    assert result["state"] == state and result["drift"] is None


def test_missing_and_legacy_not_canonical(canonical):
    assert report.compare(canonical, None)["drift"] is None
    assert report.compare(canonical, {"stripe": "done"})["drift"] is None


def test_reports_are_retained_per_surface(tmp_path, canonical):
    for surface, stripe in (("row", "done"), ("tab", "waiting")):
        report.record(tmp_path, "s", stripe, [], observation=paint(canonical, surface=surface, stripe=stripe))
    result = report.surface_observations(tmp_path, ["s"], canonical)
    assert result["row"]["drift"] is False
    assert result["tab"]["drift"] is True
    assert result["dot"]["drift"] is None


def test_unknown_source_status_still_has_exact_paint_parity(canonical):
    canonical.update(kind='unresolved', coverage={'state': 'unknown', 'reasons': ['cards:unknown']},
                     presentation={'stripe': 'attention', 'reason_codes': ['source_unknown']})
    assert report.compare(canonical, paint(canonical, stripe='attention'))['drift'] is False
    assert report.compare(canonical, paint(canonical, stripe='done'))['drift'] is True


def test_unmounted_missing_and_expired_are_unknown(canonical):
    for changes in ({"mounted": False}, {"mounted": None}, {"observation_order": None}, {"at": 1}):
        result = report.compare(canonical, paint(canonical, **changes))
        assert result["state"] == "unknown" and result["drift"] is None


def test_delayed_mount_cannot_override_new_lifetime(tmp_path, canonical):
    for changes in ({"mounted": False, "observation_order": 2}, {},
                    {"observation_lifetime": 2, "observation_order": 3, "stripe": "waiting"},
                    {"observation_order": 4}):
        obs = paint(canonical, **changes)
        report.record(tmp_path, "s", obs["stripe"], [], observation=obs)
    result = report.surface_observations(tmp_path, ["s"], canonical)["row"]
    assert result["state"] == "mismatch"
    assert result["observation"]["observation_lifetime"] == 2


def test_delayed_mount_cannot_resurrect_unmounted(tmp_path, canonical):
    for obs in (paint(canonical, mounted=False, observation_order=2), paint(canonical)):
        report.record(tmp_path, "s", obs["stripe"], [], observation=obs)
    assert report.surface_observations(tmp_path, ["s"], canonical)["row"]["state"] == "unknown"


def test_same_revision_arrival_order_is_not_paint_order(tmp_path, canonical):
    for changes in ({"stripe": "waiting", "observation_order": 3}, {"observation_order": 2}):
        obs = paint(canonical, **changes)
        report.record(tmp_path, "s", obs["stripe"], [], observation=obs)
    result = report.surface_observations(tmp_path, ["s"], canonical)["row"]
    assert result["state"] == "mismatch"
    assert result["observation"]["observation_order"] == 3


@pytest.mark.parametrize("other", [{"observation_lifetime": 2}, {"observation_client": "client-b"}])
def test_each_mount_has_independent_custody(tmp_path, canonical, other):
    # The newer mount unmounting must not hide a still-mounted mismatching surface.
    events = [paint(canonical, stripe="waiting"), paint(canonical, **other),
              paint(canonical, mounted=False, observation_order=4, **other)]
    for obs in events:
        report.record(tmp_path, "s", obs["stripe"], [], observation=obs)
    result = report.surface_observations(tmp_path, ["s"], canonical)["row"]
    assert result["state"] == "mismatch" and result["drift"] is True
    assert len(result["instances"]) == 2


@pytest.mark.parametrize("changes", [{"observation_client": None}, {"observation_client": ""},
                                     {"observation_lifetime": -1}, {"observation_order": True},
                                     {"mounted": "true"}])
def test_malformed_lifecycle_never_certifies(canonical, changes):
    assert report.compare(canonical, paint(canonical, **changes))["drift"] is None


def test_unmount_is_terminal_even_if_bad_mount_has_higher_order(tmp_path, canonical):
    for obs in (paint(canonical, mounted=False, observation_order=2),
                paint(canonical, observation_order=3)):
        report.record(tmp_path, "s", obs["stripe"], [], observation=obs)
    assert report.surface_observations(tmp_path, ["s"], canonical)["row"]["drift"] is None


def test_rest_keeps_lifecycle_and_evaluates_ordered_paint(tmp_path, canonical, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from hermes_cli.web_models import SessionStripeReport
    from hermes_cli.web_routers import sessions
    import hermes_state_outcome

    db = SimpleNamespace(db_path=str(tmp_path / "state.db"))
    monkeypatch.setattr(sessions, "_with_db", lambda profile, fn, **kw: fn(db))
    monkeypatch.setattr(sessions, "_resolve_session_id", lambda db, sid: sid)
    # The envelope is the stored outcome's `status`; the legacy `kind` beside it is never compared.
    monkeypatch.setattr(hermes_state_outcome, "get_outcome",
                        lambda sid, db: {"kind": "unresolved", "status": canonical})
    async def post(**changes):
        return await sessions.stripe_report_endpoint("s", SessionStripeReport(**paint(canonical, **changes)))
    result = asyncio.run(post(mounted=False, observation_order=3))
    assert result["report"]["observation_client"] == "client-a"
    assert result["comparison"]["drift"] is None
    result = asyncio.run(post(observation_order=2))
    assert result["comparison"]["drift"] is None
    result = asyncio.run(post(observation_client="client-b", stripe="waiting"))
    assert result["comparison"]["drift"] is True


def test_instance_budget_fails_closed(tmp_path, canonical, monkeypatch):
    monkeypatch.setattr(report, "MAX_OBSERVATION_INSTANCES", 2)
    for lifetime in range(3):
        report.record(tmp_path, "s", "done", [], observation=paint(canonical, observation_lifetime=lifetime))
    result = report.surface_observations(tmp_path, ["s"], canonical)["row"]
    assert len(result["instances"]) == 2
    assert result["state"] == "unknown" and result["drift"] is None
    assert result["reason"] == "observation_budget_exceeded"


def test_stale_late_paint_invalidates_instead_of_certifying(tmp_path, canonical):
    for changes in ({}, {"revision": 3, "observation_order": 2}):
        report.record(tmp_path, "s", "done", [], observation=paint(canonical, **changes))
    result = report.surface_observations(tmp_path, ["s"], canonical)["row"]
    assert result["state"] == "stale" and result["drift"] is None


@pytest.mark.parametrize("changes", [{"mounted": "false"}, {"observation_order": True},
                                     {"observation_lifetime": -1}, {"observation_client": ""}])
def test_rest_rejects_coerced_lifecycle(changes, canonical):
    from pydantic import ValidationError
    from hermes_cli.web_models import SessionStripeReport
    with pytest.raises(ValidationError):
        SessionStripeReport(**paint(canonical, **changes))


def test_a_local_override_is_neither_agreement_nor_drift(canonical):
    """A surface painting its own live turn over a revision says so; it is not that revision's paint."""
    from hermes_cli.desktop_stripe_report import compare

    for stripe in ("none", "done", "stuck"):
        verdict = compare(canonical, paint(canonical, stripe=stripe, local_override="live_turn"))
        assert verdict == {"state": "unknown", "reason": "local_override", "drift": None}
    # A wrong owner is still refused before anything else is read.
    foreign = {**canonical["identity"], "owner_epoch": "other"}
    assert compare(canonical, paint(canonical, identity=foreign, local_override="live_turn"))["state"] == "refused"


def test_a_committed_paint_is_never_the_red_reasons_report(tmp_path, canonical):
    """`latest` answers "why are you red" from the reasons reports only."""
    from hermes_cli.desktop_stripe_report import latest, record

    record(tmp_path, "s", "stuck", ["kanban_blocked"], now=1.0)
    record(tmp_path, "s", "done", [], observation=paint(canonical), now=2.0)
    assert latest(tmp_path, ["s"])["stripe"] == "stuck"
    assert latest(tmp_path, ["s"])["reasons"] == ["kanban_blocked"]


def test_rest_reasons_report_without_a_surface_is_recorded_and_never_compared(tmp_path, monkeypatch):
    """The red-edge reasons report (an unqualified body) keeps working and reads no status."""
    import asyncio
    from types import SimpleNamespace
    from hermes_cli.web_models import SessionStripeReport
    from hermes_cli.web_routers import sessions
    import hermes_state_outcome

    db = SimpleNamespace(db_path=str(tmp_path / "state.db"))
    monkeypatch.setattr(sessions, "_with_db", lambda profile, fn, **kw: fn(db))
    monkeypatch.setattr(sessions, "_resolve_session_id", lambda db, sid: sid)
    monkeypatch.setattr(hermes_state_outcome, "get_outcome", lambda *a, **k: pytest.fail("status was read"))
    body = SessionStripeReport(stripe="stuck", previous="done", reasons=["kanban_blocked"])
    result = asyncio.run(sessions.stripe_report_endpoint("s", body))
    assert result == {"ok": True, "report": report.latest(tmp_path, ["s"])}
    assert result["report"]["reasons"] == ["kanban_blocked"] and "surface" not in result["report"]
