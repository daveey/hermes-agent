"""Ready-wait cohorts and the four gates, from hand-built capture records. [daveey]

Times are seconds after the window opens; ``clock_error_s`` is 0 unless a test is about it, so each threshold
is checked exactly on and just past its boundary.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_fairness_metrics as metrics
from hermes_cli.kanban_fairness_schema import BadRecord, decode_owner, decode_record

T0 = 1_795_000_000
PREFIXES = ("worker-claim-watch:", "pickup-stall:", "mods-suite-red:")


class Log:
    def __init__(self):
        self.records = []

    def _add(self, kind, at, tid, op, fields):
        self.records.append({"seq": len(self.records) + 1, "kind": kind, "wall_ms": (T0 + at) * 1000,
                             "task_id": tid, "op": op, "fields": fields})

    def row(self, at, tid, status, *, priority=0, since=None, key=None, kind="task", op="update"):
        since = at if since is None and status == "ready" else since
        self._add(kind, at, tid, None if kind == "opening" else op, {
            "status": status, "priority": priority, "idempotency_key": key,
            "ready_since": None if since is None else (T0 + since) * 1000, "created_at": T0})

    def wait(self, tid, start, end, *, priority=0, key=None, into="running"):
        """One ready episode from ``start`` to ``end``, claimed unless ``into`` says otherwise."""
        self.row(start, tid, "ready", priority=priority, key=key, op="insert")
        if into == "running":
            self._add("event", end, tid, None, {"kind": "claimed", "created_at": T0 + end})
        self.row(end, tid, into, priority=priority, key=key, since=start)

    def capture(self, close, *, owners=None, gaps=(), **assumptions):
        ordered = sorted(self.records, key=lambda r: r["wall_ms"])
        for seq, record in enumerate(ordered, 1):
            record["seq"] = seq
        return {"manifest": {"capture_id": "t", "board": "default", "policy": {},
                             "window": {"armed_ms": T0 * 1000, "sealed_ms": (T0 + close) * 1000},
                             "assumptions": {"clock_error_s": 0, **assumptions}},
                "records": ordered, "owners": owners, "gaps": list(gaps)}


def _gates(capture, **kw):
    return metrics.verdict(capture, alarm_prefixes=PREFIXES, **kw)["gates"]


def _owner_cover(close):
    return [{"kind": "coverage", "source": source, "from_s": T0 - 1.0, "to_s": T0 + close + 1.0,
             "complete": True, "reason": None} for source in ("core-lane", "state-turn")]


@pytest.mark.parametrize("waits, status, median", [
    ([100, 1800, 5000], "PASS", 1800),          # odd count: the middle one, exactly on the threshold
    ([100, 1801, 5000], "FAIL", 1801),
    ([1700, 1900], "PASS", 1800),               # even count: mean of the central two
    ([1700, 1902], "FAIL", 1801),
])
def test_p0_median_boundary(waits, status, median):
    log = Log()
    for i, wait in enumerate(waits):
        log.wait(f"t{i}", 10 * i, 10 * i + wait)
    log.wait("p1000", 0, 6000, priority=1000)  # not in the p0 cohort
    gate = _gates(log.capture(9000))["p0_median_wait"]
    assert (gate["status"], gate["median_lo_s"], gate["median_hi_s"], gate["episodes"]) == (
        status, median, median, len(waits))


def test_p0_median_with_unbounded_episodes():
    log = Log()
    log.wait("a", 0, 100)
    log.wait("b", 0, 200)
    log.row(8000, "c", "ready", op="insert")  # arrives at the close edge, never claimed
    gate = _gates(log.capture(8100))["p0_median_wait"]
    assert (gate["status"], gate["median_hi_s"], gate["open"]) == ("PASS", 200, 1)
    log.row(8050, "d", "ready", op="insert")  # now the upper median is one of the unbounded ones
    gate = _gates(log.capture(8100))["p0_median_wait"]
    assert (gate["status"], gate["median_hi_s"]) == ("INDETERMINATE", None)


def test_empty_cohorts_are_indeterminate():
    log = Log()
    log.wait("p5", 0, 10, priority=5)
    gates = _gates(log.capture(100, owners=_owner_cover(100)))
    assert {gates[name]["status"] for name in ("p0_median_wait", "alarm_wait", "owner_wait")} == {"INDETERMINATE"}
    assert gates["max_wait_while_claims"]["status"] == "PASS"


@pytest.mark.parametrize("wait, status", [(7200, "PASS"), (7201, "FAIL")])
def test_max_wait_boundary_at_its_own_claim(wait, status):
    log = Log()
    log.wait("slow", 0, wait)
    gate = _gates(log.capture(8000))["max_wait_while_claims"]
    assert gate["status"] == status and gate["raw_max_hi_s"] == wait
    assert [b["task_id"] for b in gate["breaches"]] == (["slow"] if status == "FAIL" else [])


def test_max_wait_needs_a_claim_during_the_overtime():
    log = Log()
    log.row(0, "stuck", "ready", op="insert")
    log.wait("early", 10, 20)  # a claim, but long before "stuck" was overdue
    gate = _gates(log.capture(9000))["max_wait_while_claims"]
    assert gate["status"] == "PASS" and gate["raw_max_lo_s"] == 9000
    log.wait("late", 8000, 8010)  # someone else was claimed while "stuck" was past 7200 s
    gate = _gates(log.capture(9000))["max_wait_while_claims"]
    assert (gate["status"], gate["breaches"][0]["task_id"]) == ("FAIL", "stuck")


def test_max_wait_without_any_claim_is_not_a_pass():
    log = Log()
    log.row(0, "stuck", "ready", op="insert")
    gate = _gates(log.capture(9000))["max_wait_while_claims"]
    assert (gate["status"], gate["claims_witnessed"]) == ("INDETERMINATE", 0)


@pytest.mark.parametrize("wait, status", [(600, "PASS"), (601, "FAIL")])
def test_alarm_boundary(wait, status):
    log = Log()
    log.wait("alarm", 0, wait, key="worker-claim-watch:w1")
    log.wait("other", 0, 5000, key="some-other-key")
    gate = _gates(log.capture(6000))["alarm_wait"]
    assert (gate["status"], gate["episodes"], gate["worst_hi_s"]) == (status, 1, wait)


def test_unclaimed_exit_has_no_upper_bound_unless_fixed_at_arm():
    log = Log()
    log.wait("alarm", 0, 30, key="pickup-stall:x", into="archived")
    gate = _gates(log.capture(100))["alarm_wait"]
    assert (gate["status"], gate["left_unclaimed"], gate["undecided"][0]["wait_hi_s"]) == ("INDETERMINATE", 1, None)
    gate = _gates(log.capture(100, unclaimed_exit="elapsed_ready"))["alarm_wait"]
    assert (gate["status"], gate["worst_hi_s"]) == ("PASS", 30)
    late = Log()
    late.wait("alarm", 0, 700, key="pickup-stall:x", into="blocked")
    assert _gates(late.capture(800))["alarm_wait"]["status"] == "FAIL"  # the lower bound alone proves it


def test_opening_backlog_keeps_its_original_ready_time():
    log = Log()
    log.row(0, "old", "ready", since=-5000, kind="opening")
    log.wait("old2", 0, 0)  # placeholder claim so the max gate has a witness
    log._add("event", 100, "old", None, {"kind": "claimed", "created_at": T0 + 100})
    log.row(100, "old", "running", since=-5000)
    verdict = metrics.verdict(log.capture(200), alarm_prefixes=PREFIXES)
    assert verdict["cohorts"]["p0"]["opening_backlog"] == 1
    assert verdict["gates"]["max_wait_while_claims"]["raw_max_hi_s"] == 5100
    assert verdict["gates"]["p0_median_wait"]["median_hi_s"] == 2550  # (0 + 5100) / 2


def test_requeue_between_sightings_is_two_episodes():
    log = Log()
    log.row(0, "t", "ready", op="insert")
    log.row(50, "t", "ready", since=40)  # ready_since moved: it left and came back unseen
    eps = metrics.episodes(log.capture(100)["records"], armed_s=T0, err=0)
    assert [(e.end, e.start_lo - T0) for e in eps] == [("requeued", 0), ("open", 40)]


def test_priority_cohort_is_fixed_at_ready_entry():
    log = Log()
    log.row(0, "t", "ready", op="insert")
    log.row(5, "t", "ready", priority=1000, since=0)  # raised by hand while waiting
    log._add("event", 10, "t", None, {"kind": "claimed", "created_at": T0 + 10})
    log.row(10, "t", "running", priority=1000, since=0)
    cohorts = metrics.verdict(log.capture(100), alarm_prefixes=PREFIXES)["cohorts"]
    assert (cohorts["p0"]["episodes"], cohorts["p0"]["priority_changed"]) == (1, 1)


def test_clock_error_and_apply_lag_widen_into_indeterminate():
    log = Log()
    log.wait("alarm", 0, 599, key="mods-suite-red:x")
    assert _gates(log.capture(700))["alarm_wait"]["status"] == "PASS"
    gate = _gates(log.capture(700, clock_error_s=2))["alarm_wait"]
    assert (gate["status"], gate["undecided"][0]["wait_hi_s"]) == ("INDETERMINATE", 601)
    # A replica shows the ready row up to 20 s late; with no ready_since the start is only bounded.
    lagged = Log()
    lagged.row(0, "seen", "todo", op="insert")
    lagged.records.append({"seq": 0, "kind": "task", "wall_ms": (T0 + 20) * 1000, "task_id": "seen", "op": "update",
                           "fields": {"status": "ready", "priority": 0, "idempotency_key": "pickup-stall:y",
                                      "ready_since": None}})
    lagged.row(610, "seen", "running")
    assert _gates(lagged.capture(700))["alarm_wait"]["worst_hi_s"] == 590
    gate = _gates(lagged.capture(700, apply_lag_s=20))["alarm_wait"]
    assert gate["status"] == "INDETERMINATE"
    assert (gate["undecided"][0]["wait_lo_s"], gate["undecided"][0]["wait_hi_s"]) == (570, 610)


def test_ready_and_claimed_between_two_sightings_still_counts():
    log = Log()
    log.row(0, "t", "todo", op="insert")
    log._add("event", 700, "t", None, {"kind": "claimed", "created_at": T0 + 700})
    log.row(720, "t", "running", since=10, key="pickup-stall:z")  # the replica never showed it ready
    log.row(900, "t", "done", since=10, key="pickup-stall:z")
    log.row(0, "was-running", "running", since=-50, kind="opening")  # claimed before the window: not an episode
    log.row(30, "was-running", "done", since=-50)
    gate = _gates(log.capture(1000, apply_lag_s=30))["alarm_wait"]
    assert (gate["status"], gate["episodes"], gate["late"][0]["wait_lo_s"]) == ("FAIL", 1, 690)


def test_owner_gate_needs_the_owner_journal():
    log = Log()
    log.wait("owner", 100, 400)
    log.wait("bystander", 0, 5000)
    held = {"kind": "interval", "source": "core-lane", "task_id": "owner", "role": "holder",
            "start_s": T0 + 50.0, "end_s": T0 + 900.0}
    gate = _gates(log.capture(6000, owners=_owner_cover(6000) + [held]))["owner_wait"]
    assert (gate["status"], gate["episodes"], gate["worst_hi_s"]) == ("PASS", 1, 300)

    gate = _gates(log.capture(6000))["owner_wait"]  # no owner_authority.jsonl at all
    assert gate["status"] == "INDETERMINATE" and "not journaled" in gate["reason"]

    broken = _owner_cover(6000)
    broken[0] = {**broken[0], "complete": False, "reason": "parentless lane commit"}
    gate = _gates(log.capture(6000, owners=broken + [held]))["owner_wait"]
    assert gate["status"] == "INDETERMINATE" and "core-lane" in gate["reason"]

    partial = [{**c, "to_s": T0 + 3000.0} for c in _owner_cover(6000)]
    assert _gates(log.capture(6000, owners=partial + [held]))["owner_wait"]["status"] == "INDETERMINATE"

    late = Log()
    late.wait("owner", 100, 800)
    gate = _gates(late.capture(6000, owners=[held]))["owner_wait"]  # a proven miss stands without coverage
    assert gate["status"] == "FAIL"


def test_coverage_gap_blocks_a_pass_but_not_a_failure():
    log = Log()
    log.wait("alarm", 0, 601, key="pickup-stall:x")
    log.wait("p0", 0, 10)
    verdict = metrics.verdict(log.capture(700, gaps=[{"code": "journal_dropped", "detail": "x"}]),
                              alarm_prefixes=PREFIXES)
    assert verdict["gates"]["alarm_wait"]["status"] == "FAIL"
    assert verdict["gates"]["p0_median_wait"]["status"] == "INDETERMINATE"
    assert (verdict["overall"], verdict["coverage"]["complete"]) == ("FAIL", False)


def test_all_gates_passing_is_the_only_overall_pass():
    log = Log()
    log.wait("p0", 0, 10)
    log.wait("alarm", 0, 10, key="pickup-stall:x", priority=5)
    log.wait("owner", 0, 10, priority=5)
    owners = _owner_cover(100) + [{"kind": "interval", "source": "state-turn", "task_id": "owner",
                                   "role": "holder", "start_s": T0 - 10.0, "end_s": None}]
    assert metrics.verdict(log.capture(100, owners=owners), alarm_prefixes=PREFIXES)["overall"] == "PASS"
    short = metrics.verdict(log.capture(100, owners=owners), alarm_prefixes=PREFIXES, min_window_s=3600)
    assert short["overall"] == "INDETERMINATE" and short["coverage"]["gaps"][0]["code"] == "window_short"


@pytest.mark.parametrize("patch", [
    {"version": 2}, {"kind": "stage"}, {"task_id": ""}, {"seq": "1"}, {"op": "upsert"},
    {"fields": {"status": "ready", "priority": "0"}}, {"fields": {"priority": 0}},
])
def test_decode_refuses_unknown_shapes(patch):
    good = {"version": 1, "seq": 1, "kind": "task", "wall_ms": 1, "task_id": "t", "op": "insert",
            "fields": {"status": "ready", "priority": 0}}
    # Legacy flat rows, even internally well-formed ones, are not versioned envelopes.
    with pytest.raises(BadRecord):
        decode_record(good)
    with pytest.raises(BadRecord):
        decode_record({**good, **patch})


def test_decode_owner_refuses_unknown_shapes():
    good = {"version": 1, "kind": "interval", "source": "core-lane", "task_id": "t", "role": "next",
            "start_s": 1.0, "end_s": None}
    assert decode_owner(good)["role"] == "next"
    for patch in ({"source": "elsewhere"}, {"role": "waiting"}, {"start_s": None}, {"kind": "generation"}):
        with pytest.raises(BadRecord):
            decode_owner({**good, **patch})
