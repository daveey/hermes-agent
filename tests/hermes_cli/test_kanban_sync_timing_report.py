"""Due-report overlap is component-evidenced, never inferred from result or duration."""
import json

import pytest

from hermes_cli import kanban_sync_timing_report as report
from tests.hermes_cli.test_kanban_sync_timing import Harness, MS


@pytest.mark.parametrize("kind", ["skipped", "none", "sent", "timeout", "raised", "send_only"])
def test_due_overlap_uses_actual_components_and_keeps_total_cohorts(tmp_path, capsys, kind):
    h = Harness(tmp_path)
    h.arm()
    h.diag.decoded_change("alpha", lambda _: None, "changes")

    def report_fn():
        if kind == "skipped":
            h.clock.ns += 1500  # A positive 1.5us overlap is not evidence of a due report.
            return False
        with h.diag.span("send" if kind == "send_only" else "collect"):
            h.clock.advance_ms(10)
            if kind == "raised":
                raise RuntimeError("collect failed")
        try:
            with h.diag.span("send"):
                h.clock.advance_ms(20)
                if kind == "timeout":
                    raise TimeoutError("send budget")
        except TimeoutError:
            return False
        return True

    if kind == "raised":
        with pytest.raises(RuntimeError):
            h.diag.report(report_fn)
    elif kind != "none":
        h.diag.report(report_fn)
    h.clock.advance_ms(5)
    h.diag.select(lambda boards, *_: boards, [("alpha", tmp_path / "a.db")], 0, lambda _: False)
    results = []
    token = h.diag.board_start("alpha", results)
    results.append("ok")
    h.diag.board_end(token, results)
    h.until[0] = None
    h.diag.boundary()

    assert report.main([str(h.log)]) == 0
    summary, = json.loads(capsys.readouterr().out)
    assert summary["valid"] and not summary["invalid"]
    served, = summary["served"]
    due = kind not in ("skipped", "none")
    total_ns = 0 if kind == "none" else 1500 if kind == "skipped" else (10 if kind == "raised" else 30) * MS
    assert served["report_overlap_ns"] == total_ns
    assert summary["cohorts"]["overlap"]["n"] == int(kind != "none")
    assert served["due_report_overlap_ns"] == (total_ns if due else 0)
    assert summary["due_report_coverage"] == ("overlap" if due else "none")
    assert summary["cohorts"]["due_report_overlap"]["n"] == int(due)
    assert summary["cohorts"]["no_due_report_overlap"]["n"] == int(not due)
    assert summary["reports"]["due"]["n"] == int(due)
    assert summary["reports"]["not_due"]["n"] == int(kind == "skipped")
    if due:
        assert summary["cohorts"]["due_report_overlap_residual_ms"]["p50"] == 5
    else:
        assert summary["cohorts"]["due_report_overlap_residual_ms"] == {"n": 0}
