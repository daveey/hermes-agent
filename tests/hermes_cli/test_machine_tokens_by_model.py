"""Bounded model token telemetry and old-Board compatibility."""
import sqlite3

import pytest

from hermes_cli import machine_drain_metrics as md
from hermes_cli import machine_throughput_metrics as mt
from hermes_cli.kanban_sync_client import HubError


def usage(root, rows, *, profile=None, legacy=False):
    path = root / "state.db" if profile is None else root / "profiles" / profile / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)")
        db.execute("INSERT INTO sessions VALUES (NULL, 3, 4, 5, 6)")
        if not legacy:
            db.execute("CREATE TABLE session_model_usage (model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)")
            db.executemany("INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?)", rows)
    return path


def assert_sums(out):
    assert sum(v[0] for v in out["tokens_by_model"].values()) == out["tokens_io_total"]
    assert sum(v[1] for v in out["tokens_by_model"].values()) == out["tokens_cache_total"]


def test_groups_all_profiles_and_unknowns(tmp_path):
    path = usage(tmp_path, [("a", 1, 2, 3, 4), ("a", 10, 20, 30, 40), (None, 1, 1, 1, 1), ("", 2, 2, 2, 2)])
    assert mt._read(path) == (39, 83, 1, 0)
    usage(tmp_path, [("a", 100, 200, 300, 400), ("b", 7, 8, 9, 10)], profile="p")
    out = mt.counters(tmp_path)
    assert out["tokens_by_model"] == {"a": [333, 777], "b": [15, 19], "unknown": [6, 6]}
    assert_sums(out)


def test_truncation_merges_collisions_and_unknown_literal(tmp_path):
    usage(tmp_path, [("x" * 64 + "a", 1, 2, 3, 4), ("x" * 64 + "b", 5, 6, 7, 8),
                     (None, 1, 0, 0, 0), ("unknown", 2, 0, 0, 0)])
    out = mt.counters(tmp_path)
    assert out["tokens_by_model"] == {"x" * 64: [14, 22], "unknown": [3, 0]}
    assert_sums(out)


@pytest.mark.parametrize("count", [32, 33, 40])
@pytest.mark.parametrize("other_io", [0, 1000])
def test_top_models_fold_without_losing_literal_other(tmp_path, count, other_io):
    usage(tmp_path, [(f"m{i:02}", i, 0, 0, 1) for i in range(count)] + [("other", other_io, 0, 0, 0)])
    out = mt.counters(tmp_path)
    models = out["tokens_by_model"]
    assert len(models) <= 32
    assert "m00" not in models and "other" in models
    assert models[f"m{count - 1:02}"] == [count - 1, 1]
    assert_sums(out)


def test_exactly_32_models_are_not_folded(tmp_path):
    usage(tmp_path, [(str(i), 1, 0, 0, 0) for i in range(32)])
    assert set(mt.counters(tmp_path)["tokens_by_model"]) == {str(i) for i in range(32)}


def test_clamped_integer_values_and_scalar_equality(tmp_path):
    usage(tmp_path, [("a", 10**15, 1, -10, 0), ("b", 10**15, 0, 10**15, 1)])
    out = mt.counters(tmp_path)
    assert all(type(n) is int and 0 <= n <= 10**15 for v in out["tokens_by_model"].values() for n in v)
    assert out["tokens_io_total"] == out["tokens_cache_total"] == 10**15
    assert_sums(out)


def test_legacy_tokens_have_unknown_model_and_duplicate_inode_not_counted(tmp_path):
    path = usage(tmp_path, [], legacy=True)
    alias = tmp_path / "profiles" / "alias" / "state.db"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(path)
    out = mt.counters(tmp_path)
    assert out["tokens_by_model"] == {"unknown": [7, 11]}
    assert_sums(out)


def test_cache_nested_values_are_not_shared(tmp_path):
    usage(tmp_path, [("a", 1, 0, 0, 0)])
    mt.reset_cache()
    try:
        first = mt.cached_counters(tmp_path, clock=lambda: 1)
        first["tokens_by_model"]["a"][0] = 999
        second = mt.cached_counters(tmp_path, clock=lambda: 2)
        assert second["tokens_by_model"] == {"a": [1, 0]}
        second["tokens_by_model"]["a"][0] = 999
        assert mt.cached_counters(tmp_path, clock=lambda: 3)["tokens_by_model"] == {"a": [1, 0]}
    finally:
        mt.reset_cache()


@pytest.mark.parametrize("message", ["unknown field(s): tokens_by_model", "invalid metrics"])
def test_400_retries_without_models_preserving_scalars(message):
    sent, rejected = [], []
    sample = {"tokens_by_model": {"a": [3, 4]}, "tokens_io_total": 3, "tokens_cache_total": 4, "drain_active": 0,
              "cpu_busy_pct": 50, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1}

    def send(value):
        sent.append(value)
        if "tokens_by_model" in value:
            raise HubError(400, message)
        return "ok"

    assert md.send_compatible(send, sample, lambda: rejected.append(True)) == "ok"
    assert sent == [sample, {"tokens_io_total": 3, "tokens_cache_total": 4, "drain_active": 0}]
    assert rejected == []


def test_read_contract_and_empty_ledger(tmp_path):
    path = usage(tmp_path, [])
    assert mt._read(path) == (0, 0, 1, 0)
    assert mt.counters(tmp_path)["tokens_by_model"] == {}


def test_ranking_uses_both_axes_and_tail_preserves_totals(tmp_path):
    usage(tmp_path, [(f"m{i:02}", i, 0, 0, 0) for i in range(33)] + [("cache", 0, 0, 100, 0)])
    out = mt.counters(tmp_path)
    models = out["tokens_by_model"]
    assert len(models) == 32 and models["cache"] == [0, 100]
    assert models["other"] == [3, 0]
    assert set(models) == {"cache", "other", *(f"m{i:02}" for i in range(3, 33))}
    assert out["tokens_io_total"] == sum(range(33))
    assert_sums(out)


@pytest.mark.parametrize("pre_drain", [False, True])
def test_pre_counter_board_keeps_legacy_fallback_and_backoff(pre_drain):
    from hermes_cli import machine_metrics as mm

    sent, now = [], [1000.0]
    sample = {"cpus": 4, "drain_active": 0, "tokens_io_total": 3, "tokens_by_model": {"a": [3, 0]}}

    def send(value):
        sent.append(value)
        allowed = {"cpus"} if pre_drain else {"cpus", "drain_active"}
        if set(value) - allowed:
            raise HubError(400, "unknown field(s): " + ", ".join(sorted(set(value) - allowed)))

    reporter = mm.Reporter(send, collect_fn=lambda: dict(sample), clock=lambda: now[0])
    assert reporter.report_due()
    assert reporter.counters_off_until == now[0] + mm.UNSUPPORTED_RETRY_S
    assert sent[-1] == ({"cpus": 4} if pre_drain else {"cpus": 4, "drain_active": 0})
    sent.clear()
    now[0] += mm.REPORT_EVERY_S
    assert reporter.report_due()
    assert all("tokens_by_model" not in value and "tokens_io_total" not in value for value in sent)


@pytest.mark.parametrize("status, calls", [(400, 2), (500, 1)])
def test_retry_bounded_and_other_errors_not_retried(status, calls):
    sent = []

    def send(value):
        sent.append(value)
        raise HubError(status, "invalid metrics")

    with pytest.raises(HubError):
        md.send_compatible(send, {"tokens_by_model": {}})
    assert len(sent) == calls
