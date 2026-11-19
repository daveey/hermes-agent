"""The LLM pacing capacity model (``kanban_llm_pacing`` v2), pure: plan, events, exchange rate. [daveey]

Phase-1 acceptance D1. Accounts are synthetic hashes (``lp.account_id``) of synthetic labels."""
from __future__ import annotations

import dataclasses

import pytest

from hermes_cli import kanban_llm_pacing as lp

NOW = 1_800_000_000.0
DAY = 86400.0
CFG = lp.PacingConfig()
R = lp.Reading
TPP = lp.Tpp(1000.0, lp.SEED)
NO_5H = lp.Tpp(None)
A, B, C = (lp.account_id(f"acct-{n}") for n in "abc")


def _pool(readings, tpp7=TPP, tpp5=NO_5H, now=NOW, cfg=CFG):
    return lp.plan_pool("anthropic", readings, tpp7, tpp5, now, cfg)[0]


def test_staggered_resets_step_the_curve_and_each_account_drains_to_reserve_at_reset_minus_margin():
    resets = {A: NOW + 2 * DAY, B: NOW + 4 * DAY, C: NOW + 6 * DAY}
    used = {A: 20.0, B: 20.0, C: 20.0}
    pool, curve = lp.plan_pool("anthropic", [R(NOW, a, lp.SEVEN_DAY, used[a], resets[a]) for a in resets],
                               TPP, NO_5H, NOW, CFG)
    step = CFG.reserve[lp.SEVEN_DAY] * TPP.value
    points = {t: capacity for t, capacity, _ in curve}
    for reset in resets.values():  # the capacity steps by one fresh reserve at every reset
        before = max(capacity for t, capacity in points.items() if t < reset)
        assert points[reset] - before == step
    assert pool["exhaust_plan_at"] == pytest.approx(max(resets.values()) - CFG.margin)

    exhausted: dict[str, float] = {}
    now = NOW
    while len(exhausted) < len(resets) and now < NOW + 7 * DAY:  # replan every read, spend the planned rate
        readings = [R(now, a, lp.SEVEN_DAY, used[a], resets[a]) for a in resets]
        plans = {p.id: p for p in (lp.plan_account(a, [r], TPP, NO_5H, now, CFG) for a, r in
                                   ((r.account, r) for r in readings))}
        now += CFG.read_interval
        for account, plan in plans.items():
            used[account] += (plan.rate or 0.0) * CFG.read_interval / TPP.value
            if account not in exhausted and used[account] >= CFG.reserve[lp.SEVEN_DAY] - 0.05:
                exhausted[account] = now
    for account, reset in resets.items():
        assert abs(exhausted[account] - (reset - CFG.margin)) <= CFG.read_interval, account


def test_an_added_account_raises_capacity_and_replans_in_the_same_read():
    before = [R(NOW, a, lp.SEVEN_DAY, 50.0, NOW + 3 * DAY) for a in (A, B)]
    now = NOW + 300
    after = [R(now, a, lp.SEVEN_DAY, 50.0, NOW + 3 * DAY) for a in (A, B)] + [R(now, C, lp.SEVEN_DAY, 0.0, None)]
    events = lp.detect_events("anthropic", after, before, [{A, B}, {A, B}], {A, B}, now)
    assert [(e["type"], e["account"]) for e in events] == [(lp.ACCOUNT_ADDED, C)]
    old, new = _pool(before), _pool(after, now=now)
    assert new["remaining_tokens"] > old["remaining_tokens"]
    assert new["rate_tokens_per_s"] > old["rate_tokens_per_s"]


def test_an_account_absent_from_three_reads_is_removed_once():
    now = NOW + 900
    current = [R(now, A, lp.SEVEN_DAY, 10.0, NOW + DAY)]
    previous = [R(NOW + 600, A, lp.SEVEN_DAY, 10.0, NOW + DAY)]
    removed = lp.detect_events("anthropic", current, previous, [{A}, {A, B}], {A, B}, now)
    assert [(e["type"], e["account"]) for e in removed] == [(lp.ACCOUNT_REMOVED, B)]
    assert not lp.detect_events("anthropic", current, previous, [{A}, {A}], {A, B}, now)  # already reported


def test_an_early_reset_is_recorded_and_raises_the_rate():
    reset = NOW + 2 * DAY
    before = [R(NOW, A, lp.SEVEN_DAY, 60.0, reset)]
    now = NOW + 300
    after = [R(now, A, lp.SEVEN_DAY, 0.0, reset)]  # used 60 -> 0 with two days left on the same window
    events = lp.detect_events("anthropic", after, before, [{A}], {A}, now)
    assert [e["type"] for e in events] == [lp.RESET_EARLY]
    moved = [R(now, A, lp.SEVEN_DAY, 60.0, reset + DAY)]  # or the reset moved by a non-scheduled amount
    assert [e["type"] for e in lp.detect_events("anthropic", moved, before, [{A}], {A}, now)] == [lp.RESET_EARLY]
    assert _pool(after, now=now)["rate_tokens_per_s"] > _pool(before)["rate_tokens_per_s"]


def test_the_scheduled_reset_is_a_reset_not_an_early_one():
    reset = NOW + 600
    before = [R(NOW, A, lp.SEVEN_DAY, 90.0, reset)]
    now = reset + 60
    after = [R(now, A, lp.SEVEN_DAY, 0.0, reset + 7 * DAY)]
    assert [e["type"] for e in lp.detect_events("anthropic", after, before, [{A}], {A}, now)] == [lp.RESET]


def test_a_pool_at_reserve_plans_rate_zero_and_holds():
    readings = [R(NOW, A, lp.SEVEN_DAY, 100.0, NOW + DAY), R(NOW, B, lp.SEVEN_DAY, 86.0, NOW + 2 * DAY),
                R(NOW, B, lp.FIVE_HOUR, 10.0, NOW + 3600)]
    pool = _pool(readings)
    assert pool["at_reserve"] is True and pool["rate_tokens_per_s"] == 0.0
    assert pool["until"] == NOW + DAY  # the earliest reset that frees an account
    # One account under reserve is enough to plan a rate (fail open).
    assert _pool(readings + [R(NOW, C, lp.SEVEN_DAY, 10.0, NOW + DAY)])["at_reserve"] is False


def test_unknown_tpp_plans_a_null_rate_and_a_seed_is_used_and_marked():
    readings = [R(NOW, A, lp.SEVEN_DAY, 10.0, NOW + 3 * DAY)]
    unknown = _pool(readings, tpp7=lp.seed_tpp("anthropic", lp.SEVEN_DAY, CFG))
    assert unknown["rate_tokens_per_s"] is None and unknown["tpp_source"] == lp.UNKNOWN_TPP
    assert unknown["at_reserve"] is False
    seeded_cfg = lp.config_from({"tokens_per_percent": {"anthropic": {"seven_day": 2.6e5, "5h": 3.9e5}}})
    seed = lp.seed_tpp("anthropic", lp.SEVEN_DAY, seeded_cfg)
    assert seed == lp.Tpp(2.6e5, lp.SEED)
    assert lp.smooth_tpp(None, None, NOW, seeded_cfg, seed) == seed  # nothing measured: the seed
    seeded = _pool(readings, tpp7=seed, cfg=seeded_cfg)
    assert seeded["tpp_source"] == lp.SEED and seeded["rate_tokens_per_s"] > 0


def test_the_five_hour_cap_binds_when_it_is_lower():
    readings = [R(NOW, A, lp.SEVEN_DAY, 10.0, NOW + 6 * DAY), R(NOW, A, lp.FIVE_HOUR, 70.0, NOW + 4 * 3600)]
    tpp7, tpp5 = lp.Tpp(6000.0, lp.MEASURED, NOW), lp.Tpp(1000.0, lp.MEASURED, NOW)
    plan = lp.plan_account(A, readings, tpp7, tpp5, NOW, CFG)
    r7 = 75.0 * 6000.0 / (6 * DAY - CFG.margin)
    r5 = 10.0 * 1000.0 / (4 * 3600)
    assert r5 < r7
    assert plan.rate == plan.rate_cap_5h == r5
    pool = _pool(readings, tpp7=tpp7, tpp5=tpp5)
    assert pool["rate_tokens_per_s"] == r5 and pool["rate_cap_5h"] == r5
    # Codex/xAI: no five-hour window, no cap.
    assert lp.plan_account(A, readings[:1], tpp7, tpp5, NOW, CFG).rate == r7


def _series(deltas, *, start_used=10.0, tokens_per_point=1000.0, reset=NOW + 5 * DAY, cross_reset_at=None):
    """One account read every 30 min; ``deltas`` percent per interval; tokens spent at ``tokens_per_point``."""
    samples, tokens = [], []
    used, cumulative, ts, current_reset = start_used, 0.0, NOW, reset
    samples.append(R(ts, A, lp.SEVEN_DAY, used, current_reset))
    tokens.append((ts, cumulative))
    for i, delta in enumerate(deltas):
        ts += 1800
        cumulative += delta * tokens_per_point
        if cross_reset_at == i:
            used, current_reset = delta, current_reset + 7 * DAY  # the window reset inside this interval
        else:
            used += delta
        samples.append(R(ts, A, lp.SEVEN_DAY, used, current_reset))
        tokens.append((ts, cumulative))
    return samples, tokens, ts


def test_the_estimator_measures_tokens_per_percent():
    samples, tokens, end = _series([2, 1, 2, 1, 2, 2])
    assert lp.tpp_estimate(samples, tokens, lp.SEVEN_DAY, end, CFG) == 1000.0


def test_the_estimator_ignores_a_span_under_five_points():
    samples, tokens, end = _series([1, 1, 0, 1, 1, 0])  # 4 points over 3 h
    assert lp.tpp_estimate(samples, tokens, lp.SEVEN_DAY, end, CFG) is None


def test_the_estimator_ignores_a_span_crossing_a_reset():
    # 30 points inside the reset interval would read as 30 x 1000 tokens bought 30 - 64 = negative percent;
    # skipped, the remaining intervals hold only 4 points: no estimate.
    samples, tokens, end = _series([1, 1, 30, 1, 1], start_used=60.0, cross_reset_at=2)
    assert lp.tpp_estimate(samples, tokens, lp.SEVEN_DAY, end, CFG) is None
    samples, tokens, end = _series([2, 2, 30, 2, 2], start_used=60.0, cross_reset_at=2)
    assert lp.tpp_estimate(samples, tokens, lp.SEVEN_DAY, end, CFG) == 1000.0  # 8 points, the reset interval out


def test_the_estimator_skips_an_account_starting_at_100_and_an_interval_with_an_event():
    samples, tokens, end = _series([2, 2, 2, 2, 2])
    capped = [R(s.ts, B, lp.SEVEN_DAY, 100.0, NOW + DAY) for s in samples]
    assert lp.tpp_estimate(samples + capped, tokens, lp.SEVEN_DAY, end, CFG) == 1000.0
    marked = lp.tpp_estimate(samples, tokens, lp.SEVEN_DAY, end, CFG, [(A, samples[3].ts)])
    assert marked == 1000.0  # the marked interval is skipped, the other 8 points still measure it
    assert lp.tpp_estimate(samples, tokens, lp.SEVEN_DAY, end, CFG, [(A, s.ts) for s in samples[2:]]) is None


def test_smoothing_moves_by_half_life_and_clamps_to_half_and_double():
    previous = lp.Tpp(1000.0, lp.MEASURED, NOW)
    half = lp.smooth_tpp(previous, 1500.0, NOW + CFG.tpp_half_life, CFG, lp.Tpp(None))
    assert half == lp.Tpp(1250.0, lp.MEASURED, NOW + CFG.tpp_half_life)
    assert lp.smooth_tpp(previous, 10_000.0, NOW + 100 * DAY, CFG, lp.Tpp(None)).value == 2000.0
    assert lp.smooth_tpp(previous, 1.0, NOW + 100 * DAY, CFG, lp.Tpp(None)).value == 500.0
    assert lp.smooth_tpp(previous, None, NOW + DAY, CFG, lp.Tpp(9.0, lp.SEED)) == previous  # last good persists


def test_v2_document_keeps_other_pools_and_computes_ratio_and_global_rate():
    cfg = dataclasses.replace(CFG, providers=("anthropic", "openai-codex"))
    codex = {"rate_tokens_per_s": 30.0, "read_at": NOW}
    first = lp.assemble_v2({"pools": {"openai-codex": codex, "gone": {"rate_tokens_per_s": 5.0}}}, "anthropic",
                           {"rate_tokens_per_s": 10.0, "read_at": NOW}, [], [{"type": "reset"}], NOW, cfg)
    assert set(first["pools"]) == {"anthropic", "openai-codex"}
    assert first["global_rate_tokens_per_s"] == 40.0
    assert lp.route_ratios(first) == {"anthropic": 0.25, "openai-codex": 0.75}
    assert first["computed_at"] == NOW and first["margin_s"] == CFG.margin
    unknown = lp.assemble_v2(first, "anthropic", {"rate_tokens_per_s": None}, [], [], NOW, cfg)
    assert unknown["global_rate_tokens_per_s"] == 30.0 and unknown["pools"]["anthropic"]["ratio"] is None
    many = lp.assemble_v2(first, "anthropic", {"rate_tokens_per_s": 1.0}, [], [{"type": "x"}] * 60, NOW, cfg)
    assert len(many["events"]) == 50


def test_config_defaults_and_removed_start_limit_keys():
    from hermes_cli.kanban_pacing_defaults import DEFAULTS
    cfg = lp.config_from(DEFAULTS)
    assert (cfg.margin, cfg.history_days, cfg.tpp_min_delta, cfg.tpp_window, cfg.tpp_half_life) == (
        10800, 8, 5.0, 86400, 43200)
    assert cfg.tokens_per_percent == {} and cfg.budget_publish is False
    assert cfg.reserve == {lp.FIVE_HOUR: 80.0, lp.SEVEN_DAY: 85.0}
    assert "pace_starts" not in DEFAULTS and "deferrable_share" not in DEFAULTS
    assert not hasattr(cfg, "pace_starts") and not hasattr(lp, "admit_limit")
