"""Bounded, application-correlated kanban sync timing diagnostics (``hermes_cli.kanban_sync_timing``).

Fixed before implementation (parent plan TIMING-DIAGNOSTICS-PLAN.md § Tests): off/default/expired/nonfinite/overlong
config changes nothing and writes nothing; activation is bounded to 600 s; a decoded change is matched to the actual
service of its own board (changes during a round stay pending, failures never read as a successful match); real
threaded callbacks race the flush without loss; overflow, file cap and write failure invalidate the capture
explicitly; records carry no names, URLs or messages.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_sync_timing as timing
from hermes_cli import kanban_sync_timing_report as report
from hermes_cli.kanban_sync_work import SyncWork

SHA = "0123456789abcdef0123456789abcdef01234567"
MS = 1_000_000


class Clock:
    """One fake time source: monotonic ns and wall seconds advance together unless a test moves one alone."""

    def __init__(self) -> None:
        self.ns = 5_000_000_000_000
        self.wall_s = 1_800_000_000.0

    def __call__(self) -> int:
        return self.ns

    def wall(self) -> float:
        return self.wall_s

    def advance_ms(self, ms: float) -> None:
        self.ns += int(ms * MS)
        self.wall_s += ms / 1000


def _doc(until):
    return {"kanban": {"sync": {"enabled": True, "diagnostics": {"timing_until": until}}}}


class Harness:
    def __init__(self, tmp_path: Path, until=None, **kw) -> None:
        self.clock = Clock()
        self.until = [until]
        self.raise_on_read: list[BaseException] = []
        self.root = tmp_path

        def read_doc(path: Path):
            assert path == tmp_path / "config.yaml"
            if self.raise_on_read:
                raise self.raise_on_read[0]
            return _doc(self.until[0])

        kw.setdefault("code_identity", lambda: (SHA, "boot", False))
        self.diag = timing.TimingDiag(root=tmp_path, read_doc=read_doc, clock_ns=self.clock,
                                      wall=self.clock.wall, **kw)

    @property
    def log(self) -> Path:
        return timing.log_path(self.root)

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def arm(self, seconds: float = 300.0) -> None:
        self.until[0] = self.clock.wall_s + seconds
        self.diag.boundary()
        assert self.diag.capture is not None


def _exercise_every_hook(diag: timing.TimingDiag) -> list:
    """Drive each product-facing hook once; return what each wrapped callable returned through the hook."""
    calls = []
    boards = [("alpha", Path("alpha.db"))]
    out = [diag.decoded_change("alpha", lambda b: calls.append(("changed", b)) or "c", "changes")]
    sentinel = [("alpha", Path("alpha.db"))]
    out.append(diag.select(lambda bs, now, pending: (calls.append(("select", pending([bs[0][1]]))), sentinel)[1],
                           boards, 1.0, lambda paths: False))
    results: list = []
    token = diag.board_start("alpha", results)
    results.append("r")
    diag.board_end(token, results)
    out.append(diag.report(lambda: calls.append("report") or True))
    with diag.span("send"):
        calls.append("span")
    diag.note_cpu_sample({"cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1})
    out.append(diag.wait(lambda *a: calls.append(("wait", a)) or "period", "x", 2))
    diag.stop("loop_exit")
    return [out, calls]


@pytest.mark.parametrize("until", [
    None, "1800000100", True, math.nan, math.inf, -math.inf, 1_799_999_999.0,  # past
    1_800_000_000.0 + 600.001,  # overlong: never clamped into a capture
    {"at": 1}, [1],
])
def test_off_or_invalid_config_writes_nothing_and_passes_every_call_through(tmp_path, until):
    h = Harness(tmp_path, until)
    threads = threading.active_count()
    h.diag.boundary()
    assert h.diag.capture is None
    got = _exercise_every_hook(h.diag)
    assert got == [["c", [("alpha", Path("alpha.db"))], True, "period"],
                   [("changed", "alpha"), ("select", False), "report", "span", ("wait", ("x", 2))]]
    assert not (tmp_path / "logs").exists()
    assert threading.active_count() == threads


@pytest.mark.parametrize("failure", [FileNotFoundError("absent"), OSError("eio"), ValueError("yaml"),
                                     RuntimeError("backend")])
def test_unreadable_config_is_off(tmp_path, failure):
    h = Harness(tmp_path, 1_800_000_100.0)
    h.raise_on_read.append(failure)
    h.diag.boundary()
    assert h.diag.capture is None
    _exercise_every_hook(h.diag)
    assert not (tmp_path / "logs").exists()


def test_malformed_config_tree_is_off(tmp_path):
    h = Harness(tmp_path)
    h.diag.read_doc = lambda _p: {"kanban": {"sync": "on"}}
    h.diag.boundary()
    assert h.diag.capture is None
    h.diag.read_doc = lambda _p: ["not", "a", "mapping"]
    h.diag.boundary()
    assert h.diag.capture is None
    assert not (tmp_path / "logs").exists()


def test_exactly_600_s_arms_and_the_monotonic_bound_stops_it_even_if_wall_time_stalls(tmp_path):
    h = Harness(tmp_path)
    h.arm(600.0)
    start = h.records()[0]
    assert start["ev"] == "start" and start["code"] == SHA and start["code_src"] == "boot"
    assert start["deadline_ns"] - start["t"] == 600 * 1_000_000_000
    h.clock.ns += 599_999 * MS  # wall clock frozen: only the monotonic bound can end this
    h.diag.boundary()
    assert h.diag.capture is not None
    h.clock.ns += 1 * MS
    h.diag.boundary()
    assert h.diag.capture is None
    stop = h.records()[-1]
    assert stop["ev"] == "stop" and stop["reason"] == "bound" and stop["valid"] is True
    # The same configured value never re-arms: a finished capture cannot silently restart.
    h.diag.boundary()
    assert h.diag.capture is None
    assert h.records()[-1] == stop


def test_overlong_value_never_arms_later_when_it_comes_within_600_s(tmp_path):
    h = Harness(tmp_path, 1_800_000_000.0 + 700)
    h.diag.boundary()
    h.clock.advance_ms(200_000)  # now only 500 s ahead
    h.diag.boundary()
    assert h.diag.capture is None
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("change,reason,valid", [
    ("expire", "expired", True), ("disable", "disabled", True), ("read_error", "config_error", False),
])
def test_capture_stops_with_an_explicit_marker(tmp_path, change, reason, valid):
    h = Harness(tmp_path)
    h.arm(5.0)
    if change == "expire":
        h.clock.advance_ms(5000)
    elif change == "disable":
        h.until[0] = None
    else:
        h.raise_on_read.append(OSError("eio"))
    h.diag.boundary()
    assert h.diag.capture is None
    stop = h.records()[-1]
    assert (stop["ev"], stop["reason"], stop["valid"]) == ("stop", reason, valid)
    size = h.log.stat().st_size
    _exercise_every_hook(h.diag)  # stopped: every hook is a pass-through again and writes nothing
    assert h.log.stat().st_size == size


def _round(h: Harness, work: SyncWork, boards, outcomes: dict, during: dict | None = None,
           pending=lambda _paths: False) -> list:
    """One sync round through the real SyncWork selection: per-board service with ``outcomes[slug]`` in
    {"ok", "error", "raise"}; ``during[slug]`` runs inside that board's service."""
    selected = h.diag.select(work.select, boards, h.clock.ns / 1e9, pending)
    results: list = []
    for slug, _path in selected:
        token = h.diag.board_start(slug, results)
        try:
            h.clock.advance_ms(50)
            (during or {}).get(slug, lambda: None)()
            if outcomes.get(slug, "ok") == "raise":
                raise RuntimeError("crash")
            results.append(slug if outcomes.get(slug, "ok") == "ok" else "error")
        except RuntimeError:
            pass
        finally:
            h.diag.board_end(token, results, error=lambda r: r == "error")
    for slug, _path in selected:
        work.finished(slug, outcomes.get(slug, "ok") == "ok")
    return [s for s, _ in selected]


def _change(h: Harness, work: SyncWork, slug: str) -> None:
    h.diag.decoded_change(slug, work.changed, "changes")


def test_decoded_changes_match_their_own_boards_actual_service(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db")), ("beta", Path("b.db"))]
    for slug, _ in boards:
        work.feed_state(slug, True)
    _round(h, work, boards, {})  # first round: the startup recovery audit services every board
    h.clock.advance_ms(10)
    _change(h, work, "alpha")  # chg A1, before the round
    # Round 1: alpha is serviced; A2 arrives during alpha's own service and must stay pending.
    assert _round(h, work, boards, {}, during={"alpha": lambda: _change(h, work, "alpha")}) == ["alpha"]

    def report_fn():
        h.clock.advance_ms(300)
        _change(h, work, "beta")  # B1 during the report
        h.clock.advance_ms(1200)
        return True
    assert h.diag.report(report_fn) is True
    # Round 2: beta fails, alpha (A2) succeeds; beta's failure keeps B1 pending, alpha never consumes it.
    assert _round(h, work, boards, {"beta": "error"}) == ["alpha", "beta"]
    h.clock.advance_ms(5000)  # the poll period re-selects the failed board
    assert _round(h, work, boards, {}) == ["beta"]
    h.until[0] = None
    h.diag.boundary()

    recs = h.records()
    chg = {r["seq"]: r for r in recs if r["ev"] == "chg"}
    a1, a2, b1 = sorted(chg)
    svc = [r for r in recs if r["ev"] == "svc" and "c_first" in r]
    ords = {chg[a1]["b"], chg[b1]["b"]}
    assert len(ords) == 2 and chg[a2]["b"] == chg[a1]["b"]
    alpha, beta = chg[a1]["b"], chg[b1]["b"]
    assert [(r["b"], r["out"], r["c_first"], r["c_last"], r["c_n"], r["fails"]) for r in svc] == [
        (alpha, "ok", a1, a1, 1, 0), (alpha, "ok", a2, a2, 1, 0),
        (beta, "error", b1, b1, 1, 0), (beta, "ok", b1, b1, 1, 1)]
    summary = report.summarize(report.captures(recs)[0])
    assert summary["valid"] is True, summary["invalid"]
    served = {s["seq"]: s for s in summary["served"]}
    assert set(served) == {a1, a2, b1}
    # B1 arrived 300 ms into a 1500 ms report: 1200 ms of its latency overlaps the report block. A2 was still
    # pending (it arrived during round 1's service) for the whole report; A1 was serviced before it began.
    assert served[b1]["report_overlap_ns"] == 1200 * MS
    assert served[a2]["report_overlap_ns"] == 1500 * MS
    assert served[a1]["report_overlap_ns"] == 0
    assert served[b1]["failed_attempts"] == 1 and served[a2]["failed_attempts"] == 0
    rep = next(r for r in recs if r["ev"] == "rep")
    assert rep["t1"] - rep["t0"] == 1500 * MS
    for seq, svc_rec in ((a1, svc[0]), (a2, svc[1]), (b1, svc[3])):
        assert served[seq]["latency_ns"] == svc_rec["t1"] - chg[seq]["t"]
    assert summary["coverage"] == "overlap"
    assert summary["cohorts"]["overlap"]["n"] == 2 and summary["cohorts"]["non_overlap"]["n"] == 1
    residuals = sorted((served[s]["latency_ns"] - served[s]["report_overlap_ns"]) / MS for s in (a2, b1))
    assert summary["cohorts"]["overlap_residual_ms"]["min"] == pytest.approx(residuals[0])
    assert summary["cohorts"]["overlap_residual_ms"]["max"] == pytest.approx(residuals[1])
    assert summary["service_failures"] == 1


def test_an_interleaved_other_board_change_is_not_served_by_this_boards_claim(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db")), ("beta", Path("b.db"))]
    for slug, _ in boards:
        work.feed_state(slug, True)
    _round(h, work, boards, {})
    for slug in ("alpha", "beta", "alpha"):
        _change(h, work, slug)
    assert _round(h, work, boards, {"beta": "raise"}) == ["alpha", "beta"]
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    a1, b1, a2 = (r["seq"] for r in recs if r["ev"] == "chg")
    alpha_svc = next(r for r in recs if r["ev"] == "svc" and r.get("out") == "ok" and "c_first" in r)
    assert (alpha_svc["c_first"], alpha_svc["c_last"], alpha_svc["c_n"]) == (a1, a2, 2)
    summary = report.summarize(report.captures(recs)[0])
    assert sorted(s["seq"] for s in summary["served"]) == [a1, a2]
    assert summary["unserviced"] == 1 and b1 not in {s["seq"] for s in summary["served"]}


def test_a_raised_service_is_no_match_and_keeps_the_change_pending_to_the_stop_marker(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db"))]
    work.feed_state("alpha", True)
    _round(h, work, boards, {})
    _change(h, work, "alpha")
    assert _round(h, work, boards, {"alpha": "raise"}) == ["alpha"]
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    svc = [r for r in recs if r["ev"] == "svc" and "c_first" in r]
    assert [r["out"] for r in svc] == ["raised"]
    pend = [r for r in recs if r["ev"] == "pend"]
    assert len(pend) == 1 and pend[0]["c_n"] == 1 and pend[0]["fails"] == 1
    summary = report.summarize(report.captures(recs)[0])
    assert summary["served"] == [] and summary["unserviced"] == 1


def test_local_pending_records_first_observation_not_each_poll(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db"))]
    work.feed_state("alpha", True)
    _round(h, work, boards, {})
    h.clock.advance_ms(7)
    first_seen = h.clock.ns
    assert _round(h, work, boards, {"alpha": "error"}, pending=lambda paths: True) == ["alpha"]
    h.clock.advance_ms(7)
    assert _round(h, work, boards, {}, pending=lambda paths: True) == ["alpha"]
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    assert [r["t"] for r in recs if r["ev"] == "loc"] == [first_seen]
    svc = [r for r in recs if r["ev"] == "svc" and "l_t" in r]
    assert [(r["out"], r["l_t"]) for r in svc] == [("error", first_seen), ("ok", first_seen)]


def test_service_from_another_thread_is_recorded_but_never_consumes_the_loops_claim(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db"))]
    work.feed_state("alpha", True)
    _round(h, work, boards, {})
    _change(h, work, "alpha")
    h.diag.select(work.select, boards, h.clock.ns / 1e9, lambda _p: False)

    def other():
        results: list = []
        token = h.diag.board_start("alpha", results)
        results.append("alpha")
        h.diag.board_end(token, results)
    t = threading.Thread(target=other)
    t.start()
    t.join(10)
    results: list = []
    token = h.diag.board_start("alpha", results)
    results.append("alpha")
    h.diag.board_end(token, results)
    h.until[0] = None
    h.diag.boundary()
    svc = [r for r in h.records() if r["ev"] == "svc"]
    assert [(r["loop"], "c_first" in r) for r in svc[-2:]] == [(False, False), (True, True)]


def test_threaded_feed_callbacks_race_the_flush_without_loss(tmp_path):
    h = Harness(tmp_path)
    h.diag.clock_ns = timing.time.monotonic_ns  # real clock for the race
    h.arm()
    work = SyncWork(5.0)
    per, threads = 400, 4
    go = threading.Event()

    def feed(slug):
        go.wait(10)
        for _ in range(per):
            h.diag.decoded_change(slug, work.changed, "changed")
    workers = [threading.Thread(target=feed, args=(f"b{i}",)) for i in range(threads)]
    for w in workers:
        w.start()
    go.set()
    while any(w.is_alive() for w in workers):
        h.diag.boundary()  # flushes while callbacks append
    for w in workers:
        w.join(10)
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    assert [r["seq"] for r in recs] == list(range(len(recs)))
    assert sum(r["ev"] == "chg" for r in recs) == per * threads
    assert recs[-1]["ev"] == "stop" and recs[-1]["lost"] == 0 and recs[-1]["valid"] is True
    assert any(r["ev"] == "flush" for r in recs)
    assert report.validate(report.captures(recs)[0]) == []


def test_queue_overflow_is_counted_and_invalidates(tmp_path):
    h = Harness(tmp_path, queue_max=3)
    h.arm()
    work = SyncWork(5.0)
    for _ in range(6):
        _change(h, work, "alpha")
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    stop = recs[-1]
    assert stop["lost"] > 0 and stop["valid"] is False
    problems = report.validate(report.captures(recs)[0])
    assert "lost_records" in problems and "sequence_gap" in problems


def test_file_cap_stops_with_marker_inside_the_cap(tmp_path):
    cap = timing.STOP_RESERVE_BYTES + 1500
    h = Harness(tmp_path, file_max=cap)
    h.arm()
    work = SyncWork(5.0)
    for _ in range(200):
        _change(h, work, "alpha")
    h.diag.boundary()
    assert h.diag.capture is None
    assert h.log.stat().st_size <= cap
    stop = h.records()[-1]
    assert (stop["ev"], stop["reason"], stop["valid"]) == ("stop", "file_cap", False)
    assert stop["lost"] > 0


def test_write_failure_stops_capture_without_breaking_sync(tmp_path):
    writes = []

    def write(fd, data):
        writes.append(len(data))
        if len(writes) > 1:  # the start record lands; the next flush hits a full disk
            raise OSError(28, "No space left on device")
        return timing.os.write(fd, data)
    h = Harness(tmp_path, write=write)
    h.arm()
    work = SyncWork(5.0)
    _change(h, work, "alpha")
    h.diag.boundary()
    assert h.diag.capture is None
    got = _exercise_every_hook(h.diag)
    assert got[0][0] == "c"
    recs = h.records()
    assert [r["ev"] for r in recs] == ["start"]
    assert report.validate(report.captures(recs)[0]) == ["no_stop_marker"]


def test_a_failing_diagnostic_never_skips_or_repeats_the_product_call(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.arm()

    def broken(*_a, **_k):
        raise KeyError("bug")
    monkeypatch.setattr(h.diag, "_note_change", broken)
    calls = []
    assert h.diag.decoded_change("alpha", lambda b: calls.append(b) or 7, "changes") == 7
    with pytest.raises(ZeroDivisionError):  # product exceptions still propagate unchanged
        h.diag.decoded_change("alpha", lambda b: 1 / 0, "changes")
    assert calls == ["alpha"]
    h.until[0] = None
    h.diag.boundary()
    stop = h.records()[-1]
    assert stop["errors"] == 2 and stop["valid"] is False


def test_records_carry_no_names_urls_or_messages(tmp_path, monkeypatch):
    from hermes_cli import machine_metrics as mm
    from hermes_cli.kanban_sync_client import HubError
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    h = Harness(tmp_path)
    h.arm()
    monkeypatch.setattr(timing, "_DIAG", h.diag)
    work = SyncWork(5.0)
    slug = "secret-board-slug"
    boards = [(slug, tmp_path / "secret-path.db")]
    _change(h, work, slug)
    _round(h, work, boards, {slug: "error"})

    def send(_sample):
        raise HubError(503, "https://secret.example.invalid/v2 task t_secret key sk-secret")
    reporter = mm.Reporter(send, collect_fn=lambda: {"cpus": 2}, cpu_limit_fn=lambda: ("off", None))
    assert h.diag.report(reporter.report_due) is False
    h.until[0] = None
    h.diag.boundary()
    text = h.log.read_text(encoding="utf-8")
    assert "secret" not in text and str(tmp_path) not in text
    for rec in h.records():
        assert set(rec) <= set(timing.FIELDS), rec
        for key, value in rec.items():
            assert isinstance(value, (int, bool)) or value in timing.ENUMS.get(key, ()) or (
                key == "code" and value == SHA), (key, value)


def _reporter_case(h: Harness, monkeypatch, send):
    from hermes_cli import machine_cpu_metrics as cpu
    from hermes_cli import machine_metrics as mm
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: h.root)
    monkeypatch.setattr(timing, "_DIAG", h.diag)

    def collect():
        h.clock.advance_ms(5)
        return {"cpus": 4}

    def sample():
        h.clock.advance_ms(1000)
        return {"cpu_busy_pct": 41.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1_800_000_001_000}
    return mm.Reporter(send, collect_fn=collect, cpu_limit_fn=lambda: (cpu.ON, 50.0), cpu_sample_fn=sample)


@pytest.mark.parametrize("failure,outcome,status", [
    (None, "ok", None), (TimeoutError("budget"), "timeout", None), ("hub", "http", 503),
    (ConnectionResetError("reset"), "os", None),
])
def test_report_record_spans_collect_sample_and_the_whole_send(tmp_path, monkeypatch, failure, outcome, status):
    from hermes_cli.kanban_sync_client import HubError
    h = Harness(tmp_path)
    h.arm()
    sent = []

    def send(sample):
        h.clock.advance_ms(400)
        sent.append(sample)
        if failure == "hub":
            raise HubError(503, "down")
        if failure is not None:
            raise failure
        return {}
    reporter = _reporter_case(h, monkeypatch, send)
    assert h.diag.report(reporter.report_due) is (failure is None)
    assert h.diag.report(reporter.report_due) is False  # not due: a quiet report is recorded too
    h.until[0] = None
    h.diag.boundary()
    reps = [r for r in h.records() if r["ev"] == "rep"]
    assert len(reps) == 2
    rep, quiet = reps
    assert rep["res"] == ("sent" if failure is None else "not_sent")
    assert rep["collect_t1"] - rep["collect_t0"] == 5 * MS
    assert rep["cpu_t1"] - rep["cpu_t0"] == 1000 * MS
    assert rep["send_t1"] - rep["send_t0"] == 400 * MS
    assert rep["send_out"] == outcome and rep.get("send_st") == status
    assert rep["cpu_win_ms"] == 1000 and rep["cpu_end_ms"] == 1_800_000_001_000
    assert rep["t1"] - rep["t0"] >= 1405 * MS
    assert rep["t0"] <= rep["collect_t0"] and rep["send_t1"] <= rep["t1"]
    assert "send_t0" not in quiet and quiet["res"] == "not_sent"
    assert len(sent) == 1


def test_reporter_outside_a_report_scope_is_unchanged(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.arm()
    reporter = _reporter_case(h, monkeypatch, lambda s: {})
    assert reporter.report_due() is True  # no report() scope: spans are no-ops
    h.until[0] = None
    h.diag.boundary()
    assert not [r for r in h.records() if r["ev"] == "rep"]


def test_flush_cost_is_recorded_as_its_own_span_outside_measured_records(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    _change(h, work, "alpha")
    h.diag.boundary()
    _change(h, work, "alpha")
    h.diag.wait(lambda *a: "remote")
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    flushes = [r for r in recs if r["ev"] == "flush"]
    assert flushes and all(r["n"] >= 1 and r["bytes"] > 0 for r in flushes)
    waits = [r for r in recs if r["ev"] == "wait"]
    assert [w["why"] for w in waits] == ["remote"]
    summary = report.summarize(report.captures(recs)[0])
    assert summary["overhead"]["flushes"] == len(flushes)


def test_analyzer_rejects_restart_and_mixed_identity(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    recs = [json.loads(line) for line in h.log.read_text(encoding="utf-8").splitlines()]
    # A second start without a stop (process restart): the first capture is incomplete.
    restarted = recs + [dict(recs[0], pid=recs[0]["pid"] + 1, run=recs[0]["run"] + 1)]
    caps = report.captures(restarted)
    assert len(caps) == 2 and "no_stop_marker" in report.validate(caps[0])
    h.until[0] = None
    h.diag.boundary()
    good = h.records()
    mixed = good[:-1] + [dict(good[-1], run=good[0]["run"] + 1)]
    assert "identity_changed" in report.validate(report.captures(mixed)[0])
    assert report.validate(report.captures(good)[0]) == []


def test_timing_key_is_a_known_config_key():
    from hermes_cli.config import _validate_config_key
    assert _validate_config_key("kanban.sync.diagnostics.timing_until") == (True, None)


def test_a_changed_value_ends_the_capture_and_arms_a_new_one(tmp_path):
    h = Harness(tmp_path)
    h.arm(30.0)
    h.until[0] = h.clock.wall_s + 300.0
    h.diag.boundary()
    recs = h.records()
    assert [r["ev"] for r in recs if r["ev"] in ("start", "stop")] == ["start", "stop", "start"]
    assert next(r for r in recs if r["ev"] == "stop")["reason"] == "disabled"
    assert h.diag.capture is not None and h.diag.capture.until == h.until[0]


def test_off_path_does_no_diagnostic_work(tmp_path):
    def clock():
        raise AssertionError("an off hook read the clock")
    h = Harness(tmp_path)
    h.diag.clock_ns = clock
    h.diag.boundary()
    assert _exercise_every_hook(h.diag)[0] == ["c", [("alpha", Path("alpha.db"))], True, "period"]


def test_change_and_selection_are_atomic(tmp_path):
    """A change whose callback is still running when the round selects is either wholly before the selection
    (dirty and claimed) or wholly after it (pending and not claimed): the selection waits for the callback."""
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db"))]
    work.feed_state("alpha", True)
    _round(h, work, boards, {})
    entered, release = threading.Event(), threading.Event()

    def slow_changed(board):
        entered.set()
        release.wait(10)
        work.changed(board)
    feed = threading.Thread(target=h.diag.decoded_change, args=("alpha", slow_changed, "changes"))
    feed.start()
    assert entered.wait(10)
    selected = []

    def loop_round():  # the round runs on its own thread: make that thread the loop's
        h.diag._loop = threading.get_ident()
        selected.append(_round(h, work, boards, {}))
    sel = threading.Thread(target=loop_round)
    sel.start()
    sel.join(0.5)
    assert sel.is_alive(), "the selection must wait for the in-flight change callback"
    release.set()
    feed.join(10)
    sel.join(10)
    assert selected == [["alpha"]]
    h.diag._loop = threading.get_ident()
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    change = next(r for r in recs if r["ev"] == "chg")
    svc = [r for r in recs if r["ev"] == "svc" and "c_first" in r]
    assert [(r["c_first"], r["out"]) for r in svc] == [(change["seq"], "ok")]
    assert not [r for r in recs if r["ev"] == "pend"]


def test_hooks_off_the_loop_thread_record_no_selection_or_report(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    work = SyncWork(5.0)
    boards = [("alpha", Path("a.db"))]
    _change(h, work, "alpha")
    got = []

    def other():
        got.append(h.diag.select(work.select, boards, 1.0, lambda _p: False))
        got.append(h.diag.report(lambda: True))
    t = threading.Thread(target=other)
    t.start()
    t.join(10)
    assert got == [boards, True]

    def report_fn():
        def off_thread_span():
            with h.diag.span("send"):
                pass
        side = threading.Thread(target=off_thread_span)
        side.start()
        side.join(10)
        with h.diag.span("bogus"):
            pass
        return True
    assert h.diag.report(report_fn) is True
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    assert not [r for r in recs if r["ev"] == "sel"]
    reps = [r for r in recs if r["ev"] == "rep"]
    assert len(reps) == 1 and not any(k.endswith("_t0") and k != "t0" for k in reps[0])
    assert recs[-1]["valid"] is True and recs[-1]["errors"] == 0
    # The loop's change was never claimed off-thread: it is still pending at the stop marker.
    assert [r["c_n"] for r in recs if r["ev"] == "pend"] == [1]


def test_a_record_outside_the_allowlist_is_dropped_and_invalidates(tmp_path):
    h = Harness(tmp_path)
    h.arm()
    cap = h.diag.capture
    with h.diag.lock:
        h.diag._emit(cap, {"ev": "chg", "b": 0, "t": 1, "slug": "secret-board"})
        h.diag._emit(cap, {"ev": "chg", "b": 0, "t": "https://secret.invalid"})
    h.until[0] = None
    h.diag.boundary()
    assert "secret" not in h.log.read_text(encoding="utf-8")
    stop = h.records()[-1]
    assert (stop["errors"], stop["lost"], stop["valid"]) == (2, 2, False)


# --- review corrections: the analyzer fails closed on file-level problems and provenance --------------------------


def _good_capture(tmp_path, **kw) -> list[dict]:
    h = Harness(tmp_path, **kw)
    h.arm()
    with h.diag.lock:
        h.diag._emit(h.diag.capture, {"ev": "chg", "b": 0, "t": h.clock.ns, "kind": "changes", "lk": 0})
    h.until[0] = None
    h.diag.boundary()
    recs = h.records()
    assert report.validate(report.captures(recs)[0]) == []
    return recs


def _run_main(tmp_path, lines: list[str], capsys) -> tuple[int, list[dict]]:
    path = tmp_path / "capture.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    code = report.main([str(path)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("prefix", [
    ["not json"],  # junk before the first start
    [json.dumps({"ev": "chg", "seq": 0, "b": 0, "t": 1, "kind": "changes", "lk": 0})],  # orphan record
    ["{\"ev\": \"svc\"}", "[]"],  # a capture whose start marker is missing
])
def test_records_before_the_first_start_are_retained_and_fail_the_file(tmp_path, capsys, prefix):
    good = [json.dumps(r) for r in _good_capture(tmp_path)]
    code, summaries = _run_main(tmp_path, prefix + good, capsys)
    assert code == 1
    assert len(summaries) == 2, "the prefix is reported, never silently dropped"
    assert summaries[0]["valid"] is False and "no_start_marker" in summaries[0]["invalid"]
    assert summaries[1]["valid"] is True


@pytest.mark.parametrize("lines", [[], ["not json"], ["{}", "{\"ev\": \"stop\"}"]])
def test_a_file_with_no_complete_capture_never_passes_and_never_crashes(tmp_path, capsys, lines):
    code, summaries = _run_main(tmp_path, lines, capsys)
    assert code == 1
    assert all(s["valid"] is False for s in summaries)


@pytest.mark.parametrize("mutate, problem", [
    (lambda recs: recs[0].update(schema=99), "schema_mismatch"),
    (lambda recs: recs[1].update(ev="mystery"), "unknown_event"),
    (lambda recs: recs[1].update(url="https://x"), "field_not_allowed"),
    (lambda recs: recs[1].update(b="board-slug"), "field_not_allowed"),
    (lambda recs: recs[0].update(code_src="none") or recs[0].pop("code"), "identity_unverified"),
    (lambda recs: recs[0].update(disk_moved=True), "identity_unverified"),
    (lambda recs: recs[0].pop("code"), "identity_unverified"),
])
def test_validate_rejects_schema_event_allowlist_and_unverified_identity(tmp_path, mutate, problem):
    recs = _good_capture(tmp_path)
    mutate(recs)
    assert problem in report.validate(report.captures(recs)[0])


def test_a_capture_without_boot_identity_is_unverified(tmp_path):

    h = Harness(tmp_path, code_identity=lambda: (None, "none", None))
    h.arm()
    h.until[0] = None
    h.diag.boundary()
    assert report.validate(report.captures(h.records())[0]) == ["identity_unverified"]


def test_malformed_but_allowlisted_records_are_invalid_not_a_crash(tmp_path, capsys):
    recs = _good_capture(tmp_path)
    i = next(i for i, r in enumerate(recs) if r["ev"] == "chg")
    recs[i].pop("t")  # a chg record with no timestamp, then a service that claims it
    recs.insert(i + 1, {"ev": "svc", "b": 0, "t0": 1, "t1": 2, "loop": True, "out": "ok",
                        "c_first": recs[i]["seq"], "c_last": recs[i]["seq"]})
    for i, r in enumerate(recs):
        r["seq"] = i
    code, summaries = _run_main(tmp_path, [json.dumps(r) for r in recs], capsys)
    assert code == 1 and "malformed_record" in summaries[0]["invalid"]


def test_unserviced_changes_and_truncated_boards_are_separate_counts(tmp_path):
    recs = _good_capture(tmp_path)
    recs.insert(-1, {"ev": "pend", "b": 0, "c_first": 1, "c_last": 3, "c_n": 3, "c_t": 1})
    for i, r in enumerate(recs):
        r["seq"] = i
    recs[-1]["pend_more"] = 5
    assert report.validate(report.captures(recs)[0]) == []
    summary = report.summarize(report.captures(recs)[0])
    assert summary["unserviced"] == 3
    assert summary["unserviced_boards_truncated"] == 5


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyError("x")])
def test_an_identity_failure_leaks_no_fd_and_still_captures_unverified(tmp_path, exc):
    import os

    def identity():
        raise exc
    before = len(os.listdir("/proc/self/fd"))
    h = Harness(tmp_path, code_identity=identity)
    h.arm()
    h.until[0] = None
    h.diag.boundary()
    assert len(os.listdir("/proc/self/fd")) == before
    recs = h.records()
    assert recs[0]["code_src"] == "none" and recs[-1]["ev"] == "stop"
    assert report.validate(report.captures(recs)[0]) == ["identity_unverified"]


def test_a_start_failure_after_open_closes_the_fd(tmp_path, monkeypatch):
    import os
    h = Harness(tmp_path)
    h.until[0] = h.clock.wall_s + 300.0
    monkeypatch.setattr(timing.random, "SystemRandom", lambda: (_ for _ in ()).throw(RuntimeError("rng")))
    before = len(os.listdir("/proc/self/fd"))
    h.diag.boundary()
    assert h.diag.capture is None
    assert len(os.listdir("/proc/self/fd")) == before
