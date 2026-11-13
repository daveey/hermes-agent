#!/usr/bin/env python3
"""Attended gateway quiet observer (quiet-v3).

Runs as the child of ``hermes kanban quiet-hold`` (so the claim-quiet lease is held for its whole
life) and decides, from cumulative CPU of the exact gateway PID, whether the gateway idles at no more
than 10% of one core over a contiguous quiet capture.

Contract (QUIET-V2-DECISIONS item 6, CONTROL-INTEGRATION-PLAN section 5):

* settle up to 120 s, then capture at least 300 s, then keep a 60 s margin, all inside the 480 s lease;
* one attempt: any invalidity ends the observation at once (best-effort ``quiet-observe-end``) and the
  process exits so the lease owner releases; nothing is retried, signalled, restarted or profiled;
* valid only if the registry epoch never changed after the capture baseline, active work stayed zero,
  there are no unknowns, the original no-work predicates stayed clean, the gateway PID/start/code stayed
  the same, and neither a gap nor a sleep/clock jump occurred (wall vs monotonic vs suspend-inclusive);
* CPU percent = 100 * delta gateway CPU seconds / elapsed monotonic seconds over the capture. The
  gateway's own handling of this observer's polls is *inside* that numerator and is never subtracted;
  the observer process's own CPU is reported separately.

A transport failure (``query_gateway_control`` returning None) is INVALID, never a fallback to scans.
Output is one typed JSON document on stdout. Exit: 0 pass, 2 fails the CPU threshold, 3 invalid.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = 1
PASS, FAIL_THRESHOLD, INVALID = "valid_pass", "valid_fail_threshold", "invalid"
_NS = 1_000_000_000
SETTLEABLE_PREFIXES = ("active_work", "predicate_nonzero:", "epoch_moved_during_read")


@dataclass(frozen=True)
class ObserverConfig:
    """Spec defaults are locked; only tests build a different one (``for_tests``)."""

    settle_max_s: float = 120.0
    capture_min_s: float = 300.0
    margin_s: float = 60.0
    lease_max_s: float = 480.0
    cpu_max_percent: float = 10.0
    poll_interval_s: float = 5.0
    max_sample_gap_s: float = 15.0
    sleep_tolerance_s: float = 1.0
    wall_tolerance_s: float = 2.0
    query_timeout_s: float = 2.0

    @classmethod
    def for_tests(cls, **overrides: Any) -> ObserverConfig:
        return replace(cls(), **overrides)


@dataclass(frozen=True)
class Sample:
    mono_ns: int
    boot_ns: int | None
    wall_s: float
    cpu_s: float | None
    pid_start: Any
    snapshot: Any  # the server result dict, or None


@dataclass(frozen=True)
class Verdict:
    state: str
    reasons: tuple[str, ...]
    capture_s: float = 0.0
    cpu_delta_s: float | None = None
    cpu_percent: float | None = None
    samples: int = 0


@dataclass(frozen=True)
class Expected:
    episode_id: str
    pid: int
    started_at: Any
    instance_id: str
    code_sha: Any
    pid_start: Any


# ---- pure checks ---------------------------------------------------------------------------

def is_settleable(reason: str) -> bool:
    return reason.startswith(SETTLEABLE_PREFIXES)


def snapshot_reasons(snap: Any, expected: Expected, *, previous_sequence: int | None = None) -> list[str]:
    """Why one server snapshot is not clean evidence (empty list = clean)."""
    if not isinstance(snap, dict):
        return ["snapshot_unavailable"]
    reasons: list[str] = []
    if snap.get("schema") != 1 or snap.get("episode_id") != expected.episode_id:
        reasons.append("snapshot_episode_mismatch")
    if snap.get("valid") is not True:
        server = snap.get("reasons")
        reasons.extend(str(r) for r in server) if isinstance(server, list) and server else \
            reasons.append("snapshot_not_valid")
    gateway = snap.get("gateway")
    if not isinstance(gateway, dict):
        reasons.append("gateway_identity_missing")
    else:
        for key, want in (("pid", expected.pid), ("started_at", expected.started_at),
                          ("instance_id", expected.instance_id), ("code_sha", expected.code_sha)):
            if gateway.get(key) != want:
                reasons.append(f"gateway_identity_changed:{key}")
    if snap.get("coverage") != "attested" or snap.get("registry_provenance") != "bootstrap":
        reasons.append("coverage_not_attested")
    if type(snap.get("activity_epoch")) is not int:
        reasons.append("epoch_missing")
    if type(snap.get("active_count")) is not int:
        reasons.append("active_count_missing")
    elif snap["active_count"] != 0:
        reasons.append("active_work")
    if snap.get("unknowns") != []:
        reasons.append("unknowns_present")
    if type(snap.get("membership_generation")) is not int:
        reasons.append("membership_generation_missing")
    predicates = snap.get("predicates")
    if not isinstance(predicates, dict):
        reasons.append("predicates_missing")
    else:
        for name in ("active_agents", "workers", "held_turn_leases"):
            value = predicates.get(name)
            if type(value) is not int:
                reasons.append(f"predicate_unreadable:{name}")
            elif value != 0:
                reasons.append(f"predicate_nonzero:{name}")
    update = snap.get("update")
    if not isinstance(update, dict) or update.get("collision") is not False:
        reasons.append("update_collision")
    sequence = snap.get("sequence")
    if type(sequence) is not int or (previous_sequence is not None and sequence <= previous_sequence):
        reasons.append("snapshot_sequence")
    return list(dict.fromkeys(reasons))


def baseline_reasons(snap: dict, baseline: dict) -> list[str]:
    """Capture-phase changes against the baseline snapshot (epoch and membership generation)."""
    reasons = []
    if snap.get("activity_epoch") != baseline.get("activity_epoch"):
        reasons.append("epoch_changed")
    if snap.get("membership_generation") != baseline.get("membership_generation"):
        reasons.append("membership_generation_changed")
    return reasons


def time_reasons(previous: Sample, current: Sample, cfg: ObserverConfig, expected: Expected) -> list[str]:
    reasons: list[str] = []
    if current.boot_ns is None or previous.boot_ns is None:
        return ["clock_unavailable"]
    dmono = (current.mono_ns - previous.mono_ns) / _NS
    dboot = (current.boot_ns - previous.boot_ns) / _NS
    dwall = current.wall_s - previous.wall_s
    if dmono <= 0:
        reasons.append("clock_not_advancing")
    if dmono > cfg.max_sample_gap_s:
        reasons.append("sampling_gap")
    if abs(dboot - dmono) > cfg.sleep_tolerance_s:
        reasons.append("sleep_detected")
    if abs(dwall - dboot) > cfg.wall_tolerance_s:
        reasons.append("wall_clock_jump")
    if current.pid_start is None or current.pid_start != expected.pid_start:
        reasons.append("gateway_process_identity_changed")
    if current.cpu_s is None or previous.cpu_s is None:
        reasons.append("cpu_unreadable")
    elif current.cpu_s < previous.cpu_s:
        reasons.append("cpu_counter_regressed")
    return reasons


def evaluate(samples: list[Sample], cfg: ObserverConfig, expected: Expected) -> Verdict:
    """Pure verdict over the recorded capture samples (first = baseline, last = end of capture)."""
    if len(samples) < 2:
        return Verdict(INVALID, ("too_few_samples",), samples=len(samples))
    reasons: list[str] = []
    baseline = samples[0].snapshot
    previous_sequence = None
    for index, sample in enumerate(samples):
        found = snapshot_reasons(sample.snapshot, expected, previous_sequence=previous_sequence)
        if isinstance(sample.snapshot, dict):
            previous_sequence = sample.snapshot.get("sequence") if type(sample.snapshot.get("sequence")) is int \
                else previous_sequence
            if index and isinstance(baseline, dict):
                found += baseline_reasons(sample.snapshot, baseline)
        if index:
            found += time_reasons(samples[index - 1], sample, cfg, expected)
        reasons += [f"sample{index}:{r}" for r in found]
    first, last = samples[0], samples[-1]
    capture_s = (last.mono_ns - first.mono_ns) / _NS
    if capture_s < cfg.capture_min_s:
        reasons.append("capture_too_short")
    cpu_delta = percent = None
    if first.cpu_s is not None and last.cpu_s is not None and capture_s > 0:
        cpu_delta = last.cpu_s - first.cpu_s
        percent = 100.0 * cpu_delta / capture_s
    if reasons or percent is None:
        return Verdict(INVALID, tuple(reasons or ["cpu_unreadable"]), capture_s, cpu_delta, percent,
                       len(samples))
    if percent > cfg.cpu_max_percent:
        return Verdict(FAIL_THRESHOLD, ("cpu_over_threshold",), capture_s, cpu_delta, percent, len(samples))
    return Verdict(PASS, (), capture_s, cpu_delta, percent, len(samples))


# ---- process readers (psutil, else /proc, else ps; Linux and macOS) -------------------------

def parse_proc_stat(text: str, clk_tck: int) -> tuple[float, int]:
    """``(utime+stime seconds, starttime ticks)`` from ``/proc/<pid>/stat`` (comm may hold spaces/parens)."""
    rest = text[text.rindex(")") + 2:].split()
    utime, stime, start = int(rest[11]), int(rest[12]), int(rest[19])
    return (utime + stime) / clk_tck, start


def parse_ps_cputime(text: str) -> float:
    """``[[dd-]hh:]mm:ss[.ff]`` as seconds (Linux and macOS ``ps -o time=`` / ``cputime=``)."""
    match = re.fullmatch(r"\s*(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*", text)
    if match is None:
        raise ValueError(f"unparseable ps cputime {text!r}")
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def read_cpu_seconds(pid: int) -> float | None:
    """Cumulative user+system CPU of exactly ``pid`` (not its children); None when unreadable."""
    try:
        import psutil
        times = psutil.Process(pid).cpu_times()
        return float(times.user + times.system)
    except ImportError:
        pass
    except Exception:  # health: allow BLE001 -- unreadable CPU invalidates the observation
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return parse_proc_stat(text, os.sysconf("SC_CLK_TCK"))[0]
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=5, check=True).stdout
        return parse_ps_cputime(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_process_start(pid: int) -> Any:
    """A stable per-process birth identity for ``pid`` (equal across reads of the same process)."""
    try:
        import psutil
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        try:
            from hermes_cli.process_identity import worker_birth
            return worker_birth(proc)
        except ImportError:
            return proc.create_time()
    except ImportError:
        pass
    except Exception:  # health: allow BLE001 -- unreadable start identity invalidates the observation (fail closed)
        return None
    try:
        return parse_proc_stat(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"), os.sysconf("SC_CLK_TCK"))[1]
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=5, check=True).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


# ---- observation ---------------------------------------------------------------------------

@dataclass
class Hooks:
    query: Callable[..., Any]
    mono_ns: Callable[[], int] = time.monotonic_ns
    boot_ns: Callable[[], int | None] = lambda: None
    wall: Callable[[], float] = time.time
    cpu_reader: Callable[[int], float | None] = read_cpu_seconds
    start_reader: Callable[[int], Any] = read_process_start
    lease_reader: Callable[[], Any] = lambda: None
    self_cpu: Callable[[], float] = time.process_time
    wait: Callable[[float], Any] = lambda seconds: threading.Event().wait(seconds)


@dataclass
class _Run:
    cfg: ObserverConfig
    hooks: Hooks
    home: Path
    episode_id: str = ""
    token: str = ""
    deadline_ns: int = 0
    expected: Expected | None = None
    log: list[dict] = field(default_factory=list)


def _params(run: _Run, **extra: Any) -> dict:
    e = run.expected
    return {"schema": 1, "episode_id": run.episode_id,
            "expected_gateway": {"pid": e.pid, "started_at": e.started_at, "instance_id": e.instance_id,
                                 "code_sha": e.code_sha},
            "deadline_mono_ns": run.deadline_ns, **extra}


def _take(run: _Run) -> Sample:
    hooks = run.hooks
    snap = hooks.query(run.home, "quiet-observe-snapshot",
                       params=_params(run, observer_token=run.token), timeout=run.cfg.query_timeout_s)
    mono, boot, wall = hooks.mono_ns(), hooks.boot_ns(), hooks.wall()
    return Sample(mono, boot, wall, hooks.cpu_reader(run.expected.pid),
                  hooks.start_reader(run.expected.pid), snap)


def _end(run: _Run) -> None:
    """Best-effort release of the server-side episode; the lease itself dies with this process."""
    if run.token:
        try:
            run.hooks.query(run.home, "quiet-observe-end", params=_params(run, observer_token=run.token),
                            timeout=run.cfg.query_timeout_s)
        except Exception as exc:  # health: allow BLE001 -- release is best effort, the verdict is already final
            logger.debug("quiet-observe-end failed (verdict unaffected): %r", exc)


def _finish(run: _Run, clock: tuple[float, int], verdict: Verdict, *, settle_s=None, samples=()) -> dict:
    """Release the episode and build the typed evidence document."""
    _end(run)
    cfg, hooks, e = run.cfg, run.hooks, run.expected
    started_self_cpu, t0 = clock
    unattested = next((sample.snapshot["unattested_code"] for sample in reversed(samples)
                       if isinstance(sample.snapshot, dict) and "unattested_code" in sample.snapshot),
                      None)
    return {
        "schema": SCHEMA, "type": "gateway_quiet_observation", "verdict": verdict.state,
        "reasons": list(verdict.reasons), "episode_id": run.episode_id or None,
        "unattested_code": unattested,
        "message": (f"extension code not attested ({unattested['count']} units); its CPU is in the numerator"
                    if verdict.state == PASS and unattested and unattested.get("count") else None),
        "gateway": None if e is None else {"pid": e.pid, "started_at": e.started_at,
                                           "instance_id": e.instance_id, "code_sha": e.code_sha},
        "thresholds": {"cpu_max_percent": cfg.cpu_max_percent, "capture_min_s": cfg.capture_min_s,
                       "settle_max_s": cfg.settle_max_s, "margin_s": cfg.margin_s,
                       "lease_max_s": cfg.lease_max_s},
        "settle_s": settle_s, "capture_s": verdict.capture_s,
        "cpu": {"delta_s": verdict.cpu_delta_s, "percent": verdict.cpu_percent,
                "includes_gateway_polling_overhead": True, "overhead_subtracted": False,
                "observer_self_cpu_s": hooks.self_cpu() - started_self_cpu},
        "samples": verdict.samples or len(samples),
        "epoch": (samples[0].snapshot or {}).get("activity_epoch") if samples else None,
        "observer_elapsed_s": (hooks.mono_ns() - t0) / _NS,
    }


def _handshake(run: _Run) -> tuple[Any, list[str] | None]:
    """Identify the gateway, read the lease, start the server-side episode.

    Returns ``(lease, None)`` on success or ``(None, reasons)`` when the observation is invalid.
    """
    cfg, hooks = run.cfg, run.hooks
    ident = hooks.query(run.home, "identify", timeout=cfg.query_timeout_s)
    if not isinstance(ident, dict) or type(ident.get("pid")) is not int or not ident.get("instance_id"):
        return None, ["gateway_identify_unavailable"]
    pid_start = hooks.start_reader(ident["pid"])
    if pid_start is None:
        return None, ["gateway_process_unreadable"]
    run.expected = Expected("", ident["pid"], ident.get("start_time"), ident["instance_id"],
                            ident.get("code_sha"), pid_start)
    reading = hooks.lease_reader()
    lease = getattr(reading, "lease", None)
    if getattr(reading, "state", None) != "active" or lease is None:
        return None, ["lease_not_active"]
    run.episode_id = lease.episode_id
    run.expected = replace(run.expected, episode_id=lease.episode_id)
    run.deadline_ns = lease.deadline_ns
    boot = hooks.boot_ns()
    if boot is None:
        return None, ["clock_unavailable"]
    if lease.deadline_ns - boot < int((cfg.capture_min_s + cfg.margin_s) * _NS):
        return None, ["lease_too_short_for_capture"]
    started = hooks.query(
        run.home, "quiet-observe-start", timeout=cfg.query_timeout_s,
        params=_params(run, producer_pid=lease.producer_pid, producer_started_at=lease.producer_started_at,
                       producer_incarnation=lease.producer_incarnation))
    if not isinstance(started, dict):
        return None, ["start_unavailable"]
    token = started.get("observer_token")
    if not isinstance(token, str) or not token:
        refusal = started.get("reasons")
        return None, list(refusal) if isinstance(refusal, list) and refusal else ["start_refused"]
    run.token = token
    return lease, None


def _settle(run: _Run) -> tuple[Sample | None, float, list[str] | None]:
    """Settle: preexisting work may finish; anything structural is fatal.

    Returns ``(baseline, settle_s, None)`` or ``(None, settle_s, reasons)``.
    """
    cfg, hooks = run.cfg, run.hooks
    settle_deadline = hooks.mono_ns() + int(cfg.settle_max_s * _NS)
    previous: Sample | None = None
    sequence = None
    first_settle = hooks.mono_ns()
    while True:
        sample = _take(run)
        found = snapshot_reasons(sample.snapshot, run.expected, previous_sequence=sequence)
        if previous is not None:
            found += [r for r in time_reasons(previous, sample, cfg, run.expected)]
        if sample.cpu_s is None:
            found.append("cpu_unreadable")
        elapsed_s = (sample.mono_ns - first_settle) / _NS
        fatal = [r for r in found if not is_settleable(r)]
        if fatal:
            return None, elapsed_s, list(dict.fromkeys(fatal))
        if isinstance(sample.snapshot, dict) and type(sample.snapshot.get("sequence")) is int:
            sequence = sample.snapshot["sequence"]
        previous = sample
        if not found:
            return sample, elapsed_s, None
        if sample.mono_ns >= settle_deadline:
            return None, elapsed_s, ["settle_timeout"]
        hooks.wait(cfg.poll_interval_s)


def _capture(run: _Run, baseline: Sample, lease: Any) -> tuple[list[Sample], list[str] | None]:
    """Capture: contiguous, any deviation ends it immediately (reasons are non-None then)."""
    cfg, hooks = run.cfg, run.hooks
    samples = [baseline]
    while True:
        remaining = cfg.capture_min_s - (samples[-1].mono_ns - baseline.mono_ns) / _NS
        if remaining <= 0:
            return samples, None
        hooks.wait(min(cfg.poll_interval_s, remaining))
        sample = _take(run)
        samples.append(sample)
        found = snapshot_reasons(sample.snapshot, run.expected,
                                 previous_sequence=samples[-2].snapshot.get("sequence")
                                 if isinstance(samples[-2].snapshot, dict) else None)
        if isinstance(sample.snapshot, dict):
            found += baseline_reasons(sample.snapshot, baseline.snapshot)
        found += time_reasons(samples[-2], sample, cfg, run.expected)
        if sample.boot_ns is not None and sample.boot_ns > lease.deadline_ns:
            found.append("lease_deadline_passed")
        if found:
            return samples, list(dict.fromkeys(found))


def observe(cfg: ObserverConfig, hooks: Hooks, home: Path) -> dict:
    """One bounded observation. Returns the typed evidence document; never retries."""
    run = _Run(cfg, hooks, Path(home))
    clock = (hooks.self_cpu(), hooks.mono_ns())

    def invalid(reasons, *, samples=(), settle_s=None) -> dict:
        return _finish(run, clock, Verdict(INVALID, tuple(reasons), samples=len(samples)),
                       samples=samples, settle_s=settle_s)

    lease, refusal = _handshake(run)
    if refusal:
        return invalid(refusal)
    baseline, settle_s, failure = _settle(run)
    if failure:
        return invalid(failure, settle_s=settle_s)
    if (lease.deadline_ns - (baseline.boot_ns or 0)) < int((cfg.capture_min_s + cfg.margin_s) * _NS):
        return invalid(["insufficient_lease_margin"], samples=[baseline], settle_s=settle_s)
    samples, failure = _capture(run, baseline, lease)
    if failure:
        return invalid(failure, samples=samples, settle_s=settle_s)
    verdict = evaluate(samples, cfg, run.expected)
    last_boot = samples[-1].boot_ns or 0
    if verdict.state != INVALID and lease.deadline_ns - last_boot < int(cfg.margin_s * _NS):
        verdict = Verdict(INVALID, ("lease_margin_lost",), verdict.capture_s, verdict.cpu_delta_s,
                          verdict.cpu_percent, verdict.samples)
    return _finish(run, clock, verdict, settle_s=settle_s, samples=samples)


def exit_code(evidence: dict) -> int:
    return {PASS: 0, FAIL_THRESHOLD: 2}.get(evidence["verdict"], 3)


def real_hooks() -> Hooks:
    from agent import claim_quiet
    from gateway.control_socket import query_gateway_control
    from hermes_platform import quiet_clock

    return Hooks(query=query_gateway_control, boot_ns=quiet_clock.suspend_inclusive_ns,
                 lease_reader=claim_quiet.inspect)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--home", help="gateway HERMES_HOME (default: this process's launch home)")
    args = parser.parse_args(argv)
    if args.home:
        home = Path(args.home)
    else:
        from gateway.status import _get_process_hermes_home
        home = _get_process_hermes_home()
    evidence = observe(ObserverConfig(), real_hooks(), home)  # spec defaults only; no CLI overrides
    sys.stdout.write(json.dumps(evidence, sort_keys=True) + "\n")
    sys.stdout.flush()
    return exit_code(evidence)


if __name__ == "__main__":
    sys.exit(main())
