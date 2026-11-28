"""Run the real latency harness inside canonical disposable-home isolation. [daveey]"""
from __future__ import annotations

import json


def test_disposable_board_latency(tmp_path, record_property):
    from scripts.session_state_latency import run

    result = run(60, 500, 0.25, str(tmp_path))
    record_property("session_state_latency", json.dumps(result, sort_keys=True))
    assert result["missed_events"] == 0
    assert result["event_to_push"]["n"] == 60
    assert result["get"]["n"] == 500
    assert result["event_to_push"]["p95_ms"] < 500, result
    assert result["get"]["p95_ms"] < 50, result
