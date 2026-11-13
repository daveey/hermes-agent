"""Registry membership, coverage generation, per-home counts, attestation hook."""
import threading

from hermes_cli.local_work import Registry
from hermes_cli.local_work_threads import TrackedThread


def test_membership_transition_bumps_generation_and_reports_pending(tmp_path):
    home = str(tmp_path / "p")
    registry = Registry(homes=[str(tmp_path / "launch")])
    before = registry.snapshot()
    token = registry.membership_begin(home, role="reserved", transition="add")
    during = registry.snapshot()
    assert during.membership_generation > before.membership_generation
    assert during.epoch > before.epoch
    assert during.pending_transitions == 1
    assert during.membership[home][0] == "starting"
    assert registry.membership_commit(token, state="ready") is True
    after = registry.snapshot()
    assert after.pending_transitions == 0
    assert after.membership[home] == ("ready", ("reserved",))
    assert after.membership_generation > during.membership_generation
    # add+remove between two samples still changes the generation
    gen = after.membership_generation
    registry.membership_commit(registry.membership_begin(home, role="reserved", transition="retire"),
                               state="removed")
    assert registry.snapshot().membership_generation > gen + 1


def test_reserve_for_unknown_and_retired_home_invalidates_not_defaults(tmp_path):
    launch, other = str(tmp_path / "launch"), str(tmp_path / "other")
    registry = Registry(homes=[launch])
    assert registry.snapshot().unknowns == ()
    generation = registry.snapshot().membership_generation
    token = registry.reserve("x", home=other, source="late")
    snap = registry.snapshot()
    assert "unknown-home" in snap.unknowns and snap.membership_generation > generation
    assert snap.per_home == {other: 1}  # counted under ITS home, never the launch home
    registry.finish(token)

    registry = Registry(homes=[launch])
    registry.membership_commit(registry.membership_begin(other, role="reserved", transition="add"), state="ready")
    registry.membership_commit(registry.membership_begin(other, role="reserved", transition="retire"),
                               state="removed")
    assert registry.snapshot().unknowns == ()
    live = registry.reserve("late-callback", home=other, source="retired")
    snap = registry.snapshot()
    assert "retired-home-reentry" in snap.unknowns and snap.active == 1
    registry.finish(live)


def test_retiring_home_keeps_live_tokens_and_commit_failure_latches_unknown(tmp_path):
    home = str(tmp_path / "h")
    registry = Registry(homes=[home])
    held = registry.reserve("turn", home=home, source="t")
    token = registry.membership_begin(home, role="launch", transition="retire")
    assert registry.snapshot().membership[home][0] == "retiring"
    registry.membership_commit(token, state="parked", coverage=False)
    snap = registry.snapshot()
    assert "membership-coverage-incomplete" in snap.unknowns
    assert snap.per_home[home] == 1 and snap.membership[home][0] == "parked"
    assert registry.membership_commit(token, state="ready") is False  # duplicate fails closed
    assert "unmatched-membership-commit" in registry.snapshot().unknowns
    registry.finish(held)


def test_component_attestation_and_code_identity_hook(tmp_path):
    registry = Registry(homes=[str(tmp_path)])
    assert registry.snapshot().code_generation is None  # no source wired: not "clean"
    generation = {"value": 7}
    registry.set_code_attestation(lambda: generation["value"])
    snap = registry.snapshot()
    assert snap.code_generation == 7
    coverage = snap.coverage_generation
    registry.attest_component("gateway", implementation_id="sha", runtime_instance="i1", capabilities=["b", "a"])
    registry.attest_component("gateway", implementation_id="sha", runtime_instance="i1", capabilities=["a", "b"])
    snap = registry.snapshot()
    assert snap.coverage_generation == coverage + 1 and snap.components["gateway"][2] == ("a", "b")
    generation["value"] = 8
    assert registry.snapshot().code_generation == 8

    def broken():
        raise RuntimeError("audit hook unreadable")
    registry.set_code_attestation(broken)
    assert "code-attestation-read-failed" in registry.snapshot().unknowns


def test_sub_sample_worker_bumps_epoch_with_zero_active(tmp_path):
    """V3 case g: a worker that starts and ends between two samples is still visible."""
    home = str(tmp_path)
    registry = Registry(homes=[home])
    before = registry.snapshot()
    ran = threading.Event()
    thread = TrackedThread(target=ran.set, registry=registry, home=home, source="short", kind="worker")
    thread.start()
    thread.join(5)
    after = registry.snapshot()
    assert ran.is_set() and before.active == after.active == 0
    assert after.epoch >= before.epoch + 2  # reserve and finish are both observable transitions
    assert after.per_home == {}
