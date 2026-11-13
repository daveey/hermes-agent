"""Gateway quiet observer: pure verdicts over recorded samples, process readers, and an end-to-end run
against a fake control endpoint with a REAL child process as the 'gateway PID' (shortened capture,
configurable for tests only; the production defaults are asserted locked)."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "observe_gateway_quiet.py"
spec = importlib.util.spec_from_file_location("observe_gateway_quiet", SCRIPT)
obs = importlib.util.module_from_spec(spec)
sys.modules["observe_gateway_quiet"] = obs
spec.loader.exec_module(obs)

NS = 1_000_000_000
CFG = obs.ObserverConfig()
EXPECTED = obs.Expected("ep-1", 4242, "start-1", "inst-1", "sha-1", "birth-1")


def snap(seq, *, epoch=7, **over):
    base = {
        "schema": 1, "episode_id": "ep-1", "phase": "snapshot", "sequence": seq, "valid": True,
        "reasons": [], "gateway": {"pid": 4242, "started_at": "start-1", "instance_id": "inst-1",
                                   "code_sha": "sha-1"},
        "coverage": "attested", "registry_provenance": "bootstrap", "activity_epoch": epoch,
        "active_count": 0, "per_kind": {}, "unknowns": [], "membership_generation": 3,
        "predicates": {"active_agents": 0, "workers": 0, "held_turn_leases": 0},
        "update": {"collision": False, "window_state": "idle"},
    }
    base.update(over)
    return base


def sample(t, cpu, seq, *, boot_offset=0.0, wall_offset=0.0, pid_start="birth-1", **snap_over):
    return obs.Sample(int(t * NS), int((t + boot_offset) * NS), 1_000_000 + t + wall_offset, cpu, pid_start,
                      snap_over.pop("_snapshot", None) or snap(seq, **snap_over))


def run_of(duration=300.0, step=5.0, cpu_per_s=0.02):
    count = round(duration / step)
    return [sample(i * step, 10 + i * step * cpu_per_s, i + 1) for i in range(count + 1)]


# ---- defaults are the spec ------------------------------------------------------------------

def test_defaults_are_locked_to_the_spec():
    assert (CFG.settle_max_s, CFG.capture_min_s, CFG.margin_s, CFG.lease_max_s, CFG.cpu_max_percent) == (
        120.0, 300.0, 60.0, 480.0, 10.0)
    assert 120 + 300 + 60 <= 480
    assert obs.ObserverConfig.for_tests(capture_min_s=1.0).capture_min_s == 1.0
    assert obs.ObserverConfig().capture_min_s == 300.0
    parser_help = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False).stdout
    assert "capture" not in parser_help.lower().split("options:")[-1]  # no CLI knob may shorten the capture


# ---- pure verdicts -------------------------------------------------------------------------

def test_clean_capture_passes_with_percent_from_cumulative_cpu():
    verdict = obs.evaluate(run_of(), CFG, EXPECTED)
    assert verdict.state == obs.PASS and verdict.reasons == ()
    assert verdict.capture_s == 300.0 and verdict.samples == 61
    assert verdict.cpu_delta_s == pytest.approx(6.0) and verdict.cpu_percent == pytest.approx(2.0)


def test_threshold_is_at_most_ten_percent_of_one_core():
    exactly = run_of()
    exactly[-1] = dataclasses.replace(exactly[-1], cpu_s=exactly[0].cpu_s + 30.0)
    assert obs.evaluate(exactly, CFG, EXPECTED).state == obs.PASS
    over = run_of()
    over[-1] = dataclasses.replace(over[-1], cpu_s=over[0].cpu_s + 30.01)
    verdict = obs.evaluate(over, CFG, EXPECTED)
    assert verdict.state == obs.FAIL_THRESHOLD and verdict.reasons == ("cpu_over_threshold",)
    assert verdict.cpu_percent > 10.0


def test_threshold_failure_is_distinct_from_invalidity():
    samples = run_of()
    samples[-1] = dataclasses.replace(samples[-1], cpu_s=samples[0].cpu_s + 100.0)
    samples[30] = dataclasses.replace(samples[30], snapshot=snap(31, epoch=8))
    verdict = obs.evaluate(samples, CFG, EXPECTED)
    assert verdict.state == obs.INVALID  # an invalid run is never reported as a clean threshold verdict


def test_short_capture_is_invalid():
    verdict = obs.evaluate(run_of(duration=295.0), CFG, EXPECTED)
    assert verdict.state == obs.INVALID and "capture_too_short" in verdict.reasons
    assert obs.evaluate(run_of(duration=300.0)[:1], CFG, EXPECTED).reasons == ("too_few_samples",)


@pytest.mark.parametrize("index, over, reason", [
    (20, {"activity_epoch": 8}, "epoch_changed"),           # even if the count is back to zero
    (20, {"active_count": 1}, "active_work"),
    (20, {"unknowns": ["late-detach"]}, "unknowns_present"),
    (20, {"membership_generation": 4}, "membership_generation_changed"),
    (20, {"membership_generation": None}, "membership_generation_missing"),
    (20, {"predicates": {"active_agents": 1, "workers": 0, "held_turn_leases": 0}},
     "predicate_nonzero:active_agents"),
    (20, {"predicates": {"active_agents": 0, "workers": None, "held_turn_leases": 0}},
     "predicate_unreadable:workers"),
    (20, {"predicates": {"active_agents": 0, "workers": 0, "held_turn_leases": 2}},
     "predicate_nonzero:held_turn_leases"),
    (20, {"update": {"collision": True}}, "update_collision"),
    (20, {"coverage": "pending"}, "coverage_not_attested"),
    (20, {"registry_provenance": "missing"}, "coverage_not_attested"),
    (20, {"valid": False, "reasons": ["epoch_moved_during_read"]}, "epoch_moved_during_read"),
    (20, {"episode_id": "other"}, "snapshot_episode_mismatch"),
])
def test_any_deviation_during_capture_invalidates(index, over, reason):
    samples = run_of()
    s = samples[index]
    changed = dict(s.snapshot)
    changed.update(over)
    samples[index] = dataclasses.replace(s, snapshot=changed)
    verdict = obs.evaluate(samples, CFG, EXPECTED)
    assert verdict.state == obs.INVALID
    assert any(reason in r for r in verdict.reasons), verdict.reasons


@pytest.mark.parametrize("key, value", [
    ("pid", 4243), ("started_at", "start-2"), ("instance_id", "inst-2"), ("code_sha", "sha-2")])
def test_gateway_identity_change_invalidates(key, value):
    samples = run_of()
    gateway = dict(samples[40].snapshot["gateway"], **{key: value})
    samples[40] = dataclasses.replace(samples[40], snapshot={**samples[40].snapshot, "gateway": gateway})
    verdict = obs.evaluate(samples, CFG, EXPECTED)
    assert verdict.state == obs.INVALID and f"gateway_identity_changed:{key}" in verdict.reasons[0] + "".join(
        verdict.reasons)


def test_process_start_change_or_unreadable_invalidates():
    samples = run_of()
    samples[10] = dataclasses.replace(samples[10], pid_start="birth-2")
    assert any("gateway_process_identity_changed" in r for r in obs.evaluate(samples, CFG, EXPECTED).reasons)
    samples = run_of()
    samples[10] = dataclasses.replace(samples[10], pid_start=None)
    assert obs.evaluate(samples, CFG, EXPECTED).state == obs.INVALID


def test_sampling_gap_sleep_and_clock_jumps_invalidate():
    gap = [sample(0, 10, 1), sample(5, 10.1, 2), sample(25, 10.2, 3)] + [
        sample(25 + 5 * i, 10.3 + i, 4 + i) for i in range(1, 56)]
    assert any("sampling_gap" in r for r in obs.evaluate(gap, CFG, EXPECTED).reasons)
    slept = run_of()
    slept[30:] = [dataclasses.replace(s, boot_ns=s.boot_ns + 8 * NS) for s in slept[30:]]  # suspend-inclusive ran ahead
    assert any("sleep_detected" in r for r in obs.evaluate(slept, CFG, EXPECTED).reasons)
    jumped = run_of()
    jumped[30:] = [dataclasses.replace(s, wall_s=s.wall_s + 30) for s in jumped[30:]]  # NTP/manual step
    assert any("wall_clock_jump" in r for r in obs.evaluate(jumped, CFG, EXPECTED).reasons)
    unavailable = run_of()
    unavailable[5] = dataclasses.replace(unavailable[5], boot_ns=None)
    assert any("clock_unavailable" in r for r in obs.evaluate(unavailable, CFG, EXPECTED).reasons)


def test_cpu_read_failure_and_counter_regression_invalidate():
    samples = run_of()
    samples[12] = dataclasses.replace(samples[12], cpu_s=None)
    assert any("cpu_unreadable" in r for r in obs.evaluate(samples, CFG, EXPECTED).reasons)
    samples = run_of()
    samples[12] = dataclasses.replace(samples[12], cpu_s=0.0)
    assert any("cpu_counter_regressed" in r for r in obs.evaluate(samples, CFG, EXPECTED).reasons)


def test_snapshot_unavailable_and_sequence_regression():
    samples = run_of()
    samples[8] = dataclasses.replace(samples[8], snapshot=None)
    assert any("snapshot_unavailable" in r for r in obs.evaluate(samples, CFG, EXPECTED).reasons)
    samples = run_of()
    samples[9] = dataclasses.replace(samples[9], snapshot={**samples[9].snapshot, "sequence": 1})
    assert any("snapshot_sequence" in r for r in obs.evaluate(samples, CFG, EXPECTED).reasons)


def test_settleable_reasons_are_only_ending_preexisting_work():
    assert obs.is_settleable("active_work") and obs.is_settleable("predicate_nonzero:workers")
    assert obs.is_settleable("epoch_moved_during_read")
    for fatal in ("unknowns_present", "update_collision", "gateway_identity_changed:pid",
                  "coverage_not_attested", "unknown:late-detach", "update:runner_draining"):
        assert not obs.is_settleable(fatal)


# ---- process readers -----------------------------------------------------------------------

def test_proc_stat_parser_handles_hostile_comm_and_units():
    fields = ["S", "1", "1", "1", "0", "-1", "4194304", "100", "0", "0", "0",
              "250", "50", "0", "0", "20", "0", "1", "0", "98765"]
    text = "4242 (weird) name (x) " + " ".join(fields) + " 0 0 0\n"
    seconds, start = obs.parse_proc_stat(text, 100)
    assert seconds == pytest.approx(3.0) and start == 98765  # (utime 250 + stime 50) / 100 Hz


@pytest.mark.parametrize("text, seconds", [
    ("0:00.12", 0.12), ("1:02.50", 62.5), ("01:02:03", 3723.0), ("2-01:00:00", 176400.0),
    (" 12:34 ", 754.0)])
def test_ps_cputime_formats(text, seconds):
    assert obs.parse_ps_cputime(text) == pytest.approx(seconds)


def test_ps_cputime_rejects_garbage():
    with pytest.raises(ValueError):
        obs.parse_ps_cputime("abc")


def test_cpu_reader_measures_exactly_the_named_pid():
    busy = subprocess.Popen([sys.executable, "-c", "import time\nt=time.process_time()\n"
                             "while time.process_time()-t<0.6: pass\nimport sys; sys.stdin.read()"],
                            stdin=subprocess.PIPE)
    try:
        before = obs.read_cpu_seconds(busy.pid)
        assert before is not None
        start = obs.read_process_start(busy.pid)
        assert start is not None and obs.read_process_start(busy.pid) == start  # stable identity
        deadline = threading.Event()
        while (obs.read_cpu_seconds(busy.pid) or 0) < 0.5 and not deadline.wait(0.05):
            pass
        assert obs.read_cpu_seconds(busy.pid) >= 0.5
        parent = obs.read_cpu_seconds(__import__("os").getpid())
        assert parent is not None and parent != obs.read_cpu_seconds(busy.pid)
    finally:
        busy.stdin.close()
        busy.wait(10)


# ---- end to end: fake control endpoint + real child as the gateway -------------------------

class FakeEndpoint:
    """Stands in for ``query_gateway_control``; every behaviour is switchable per snapshot number."""

    def __init__(self, pid, *, busy_for=0, epoch_bump_at=None, none_at=None, extra_at=None,
                 identify=True, start_token="tok", collision_at=None):
        self.pid, self.calls = pid, []
        self.busy_for, self.epoch_bump_at, self.none_at = busy_for, epoch_bump_at, none_at
        self.extra_at, self.identify, self.start_token, self.collision_at = extra_at, identify, start_token, collision_at
        self.snapshots = 0

    def __call__(self, home, verb, *, params=None, timeout=2.0, **_):
        self.calls.append(verb)
        if verb == "identify":
            return {"pid": self.pid, "start_time": "start-1", "instance_id": "inst-1", "code_sha": "sha-1"} \
                if self.identify else None
        if verb == "quiet-observe-start":
            if self.start_token is None:
                return {"valid": False, "phase": "coverage_pending", "reasons": ["coverage_pending"]}
            return {"valid": True, "observer_token": self.start_token}
        if verb == "quiet-observe-end":
            return {"valid": False, "phase": "ended"}
        assert verb == "quiet-observe-snapshot" and params["observer_token"] == "tok"
        self.snapshots += 1
        n = self.snapshots
        if self.none_at == n:
            return None
        body = snap(n, epoch=7 + (1 if self.epoch_bump_at is not None and n >= self.epoch_bump_at else 0))
        body["gateway"] = {"pid": self.pid, "started_at": "start-1", "instance_id": "inst-1", "code_sha": "sha-1"}
        if n <= self.busy_for:
            body.update(active_count=1, valid=False, reasons=["active_work"])
        if self.collision_at is not None and n >= self.collision_at:
            body.update(update={"collision": True}, valid=False, reasons=["update:runner_draining"])
        return body


@pytest.fixture
def child():
    """A real idle process standing in as the gateway PID."""
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    yield proc
    proc.stdin.close()
    proc.wait(10)


def hooks_for(endpoint, *, lease_seconds=60.0):
    from hermes_platform import quiet_clock
    if quiet_clock.suspend_inclusive_ns() is None:
        pytest.skip("no suspend-inclusive clock on this host")
    deadline = quiet_clock.suspend_inclusive_ns() + int(lease_seconds * NS)
    lease = SimpleNamespace(episode_id="ep-1", deadline_ns=deadline, producer_pid=1,
                            producer_started_at=1.0, producer_incarnation="inc")
    return obs.Hooks(query=endpoint, boot_ns=quiet_clock.suspend_inclusive_ns,
                     lease_reader=lambda: SimpleNamespace(state="active", lease=lease))


SHORT = obs.ObserverConfig.for_tests(settle_max_s=5.0, capture_min_s=1.0, margin_s=0.5, lease_max_s=60.0,
                                     poll_interval_s=0.1, max_sample_gap_s=2.0, query_timeout_s=1.0)


def test_end_to_end_idle_gateway_passes_and_reports_overhead(child):
    endpoint = FakeEndpoint(child.pid)
    evidence = obs.observe(SHORT, hooks_for(endpoint), Path("/nonexistent"))
    assert evidence["verdict"] == obs.PASS, evidence
    assert evidence["type"] == "gateway_quiet_observation" and evidence["schema"] == 1
    assert evidence["capture_s"] >= 1.0 and evidence["samples"] >= 8
    assert evidence["cpu"]["percent"] < 10.0 and evidence["cpu"]["overhead_subtracted"] is False
    assert evidence["cpu"]["includes_gateway_polling_overhead"] is True
    assert evidence["cpu"]["observer_self_cpu_s"] >= 0
    assert evidence["gateway"]["pid"] == child.pid and evidence["episode_id"] == "ep-1"
    assert endpoint.calls.count("quiet-observe-start") == 1 and endpoint.calls[-1] == "quiet-observe-end"
    json.dumps(evidence)  # typed, serialisable evidence
    assert obs.exit_code(evidence) == 0


def test_end_to_end_busy_gateway_fails_threshold():
    proc = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        evidence = obs.observe(SHORT, hooks_for(FakeEndpoint(proc.pid)), Path("/nonexistent"))
    finally:
        proc.kill()
        proc.wait(10)
    assert evidence["verdict"] == obs.FAIL_THRESHOLD, evidence
    assert evidence["cpu"]["percent"] > 10.0 and obs.exit_code(evidence) == 2


def test_settle_lets_preexisting_work_finish(child):
    endpoint = FakeEndpoint(child.pid, busy_for=3)
    evidence = obs.observe(SHORT, hooks_for(endpoint), Path("/x"))
    assert evidence["verdict"] == obs.PASS and evidence["settle_s"] >= 0.2


def test_settle_timeout_is_invalid_and_releases(child):
    endpoint = FakeEndpoint(child.pid, busy_for=10_000)
    config = obs.ObserverConfig.for_tests(**{**dataclasses.asdict(SHORT), "settle_max_s": 0.5})
    evidence = obs.observe(config, hooks_for(endpoint), Path("/x"))
    assert evidence["verdict"] == obs.INVALID and evidence["reasons"] == ["settle_timeout"]
    assert endpoint.calls[-1] == "quiet-observe-end" and obs.exit_code(evidence) == 3


@pytest.mark.parametrize("kwargs, reason", [
    ({"epoch_bump_at": 6}, "epoch_changed"),
    ({"none_at": 6}, "snapshot_unavailable"),
    ({"collision_at": 6}, "update_collision"),
])
def test_capture_invalidity_releases_immediately_without_retry(child, kwargs, reason):
    endpoint = FakeEndpoint(child.pid, **kwargs)
    evidence = obs.observe(SHORT, hooks_for(endpoint), Path("/x"))
    assert evidence["verdict"] == obs.INVALID and any(reason in r for r in evidence["reasons"]), evidence
    # Stopped at the offending snapshot: no further polling, one start, one end, no second attempt.
    assert endpoint.snapshots == 6
    assert endpoint.calls.count("quiet-observe-start") == 1 and endpoint.calls[-1] == "quiet-observe-end"
    assert endpoint.calls.count("quiet-observe-end") == 1


def test_gateway_process_replacement_during_capture_invalidates(child):
    endpoint = FakeEndpoint(child.pid)
    hooks = hooks_for(endpoint)
    reads = []

    def start_reader(pid):
        reads.append(pid)
        return "birth-1" if len(reads) < 6 else "birth-2"

    hooks.start_reader = start_reader
    evidence = obs.observe(SHORT, hooks, Path("/x"))
    assert evidence["verdict"] == obs.INVALID
    assert any("gateway_process_identity_changed" in r for r in evidence["reasons"])


def test_unreadable_cpu_invalidates_immediately(child):
    hooks = hooks_for(FakeEndpoint(child.pid))
    hooks.cpu_reader = lambda pid: None
    evidence = obs.observe(SHORT, hooks, Path("/x"))
    assert evidence["verdict"] == obs.INVALID and "cpu_unreadable" in evidence["reasons"]


@pytest.mark.parametrize("endpoint_kwargs, reason", [
    ({"identify": False}, "gateway_identify_unavailable"),
    ({"start_token": None}, "coverage_pending"),
])
def test_start_refusals_are_invalid(child, endpoint_kwargs, reason):
    endpoint = FakeEndpoint(child.pid, **endpoint_kwargs)
    evidence = obs.observe(SHORT, hooks_for(endpoint), Path("/x"))
    assert evidence["verdict"] == obs.INVALID and reason in evidence["reasons"]
    assert "quiet-observe-snapshot" not in endpoint.calls


def test_lease_missing_or_too_short_is_invalid(child):
    hooks = hooks_for(FakeEndpoint(child.pid))
    hooks.lease_reader = lambda: SimpleNamespace(state="inactive", lease=None)
    assert obs.observe(SHORT, hooks, Path("/x"))["reasons"] == ["lease_not_active"]
    short = obs.observe(SHORT, hooks_for(FakeEndpoint(child.pid), lease_seconds=1.2), Path("/x"))
    assert short["verdict"] == obs.INVALID and short["reasons"] == ["lease_too_short_for_capture"]


def test_cli_with_no_gateway_is_invalid_not_a_fallback(tmp_path, capsys):
    code = obs.main(["--home", str(tmp_path)])
    evidence = json.loads(capsys.readouterr().out)
    assert code == 3 and evidence["verdict"] == obs.INVALID
    assert evidence["reasons"] == ["gateway_identify_unavailable"]
