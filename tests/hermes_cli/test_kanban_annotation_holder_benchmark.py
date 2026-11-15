"""Decision replay must charge quiet post-floor work, not just exact moves."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def bench():
    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_kanban_annotation_holder.py"
    spec = importlib.util.spec_from_file_location("annotation_holder_bench", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def quiet_stream(count):
    return {"samples": [{"moved": {"default": []}} for _ in range(count)]}


@pytest.mark.parametrize("count,renewals,keys", [(10, 1, 5), (11, 2, 5), (20, 2, 10)])
def test_quiet_replay_probes_every_post_floor_pass(bench, count, renewals, keys):
    result = bench.replay(quiet_stream(count), 100, 0, 0, 0, local_move_rate=0)
    assert result["refreshes"] == result["renewals"] == renewals
    assert result["narrow_key_calls"] == keys
    assert result["narrow_key_pct"] == round(keys * 0.1 / (count * 2) * 100, 4)


@pytest.mark.parametrize("key_ms,exit_code", [(10, 0), (100, 1)])
def test_quiet_replay_enforces_key_cpu_threshold(bench, monkeypatch, tmp_path, capsys, key_ms, exit_code):
    union = tmp_path / "quiet.json"
    union.write_text(json.dumps(quiet_stream(10)), encoding="utf-8")
    monkeypatch.setattr(bench, "OWN_CHANGE_P", 0)
    original = bench.replay
    monkeypatch.setattr(bench, "replay", lambda *args: original(*args, local_move_rate=0))
    monkeypatch.setattr(bench, "scale_tier", lambda *_: {
        "tier": "small", "key_ms_mean": key_ms, "key_moved_without_substantive": 0,
    })
    monkeypatch.setattr(bench.sys, "argv", ["benchmark", "--tiers", "small", "--union", str(union),
                                           "--exact-move-rate", "0", "--per-refresh-ms", "0", "--probe-ms", "0"])
    assert bench.main() == exit_code
    assert ("narrow_key_pct" in capsys.readouterr().err) == bool(exit_code)
