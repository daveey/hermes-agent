"""Phase-1 acceptance D2: a week of synthetic demand replayed through the capacity model. [daveey]

The fixture (``fixtures/llm_pacing_replay_synthetic.json``, from ``llm_pacing_replay.generate``) is synthetic: it
is shaped like the hub's 11-12..11-19 week, no live data. The real-sample replay on ``llm_pacing_history.sqlite``
is fixed for day 8 after deploy and is not this test."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_llm_pacing as lp
from tests.hermes_cli import llm_pacing_replay as rp

CFG = lp.PacingConfig()
EARLY, LATE = 3600.0, 900.0


@pytest.fixture(scope="module")
def fixture():
    return json.loads(rp.FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=["anthropic", "openai-codex", "xai-oauth"])
def run(request, fixture):
    return request.param, rp.replay(fixture, request.param, CFG)


def test_the_fixture_is_the_generator_output(fixture):
    assert fixture == json.loads(json.dumps(rp.generate()))


def test_every_projected_exhaustion_lands_in_the_margin_band(run):
    """Every read's plan: each account's projected exhaustion within [reset - margin - 1 h, reset - margin + 15 min],
    and the pool's ``exhaust_plan_at`` is the latest of them."""
    provider, result = run
    checked = 0
    for read in result["projections"]:
        ends = []
        for remaining, rate, reset in read["accounts"].values():
            if rate and remaining is not None:
                exhaust = read["now"] + remaining / rate
                assert reset - CFG.margin - EARLY <= exhaust <= reset - CFG.margin + LATE, (provider, read["now"])
                ends.append(exhaust)
                checked += 1
        if ends:
            assert read["exhaust_plan_at"] == pytest.approx(max(ends))
    assert checked > 100


def test_the_exchange_rate_is_measured_from_the_replayed_spend(run, fixture):
    provider, result = run
    sources = [read["tpp_source"] for read in result["projections"]]
    assert sources[0] == lp.UNKNOWN_TPP and sources[-1] == lp.MEASURED
    # Measured once the pool spent tpp_min_delta_percent over two hours: 2 h for Anthropic and Codex, about 6 h on
    # the single xAI account (brief B: too few points to trust sooner).
    assert sources.index(lp.MEASURED) <= 12 * 8
    assert result["tpp"][lp.SEVEN_DAY].value == pytest.approx(fixture["pools"][provider]["tpp_7d"], rel=0.1)


def test_planned_windows_drain_to_reserve_by_reset_minus_margin(run, fixture):
    """The spend the plan lets through actually brings each planned weekly window to its reserve, no later than
    15 min past ``reset - margin``; every planned window due inside the week did drain."""
    provider, result = run
    drained = [(at, reset) for drains in result["drains"].values() for at, reset in drains]
    for at, reset in drained:
        assert at <= reset - CFG.margin + LATE, (provider, at, reset)
    start = fixture["start"]
    due = [a for a in fixture["pools"][provider]["accounts"]
           if a["used_7d"] < CFG.reserve[lp.SEVEN_DAY] and start + a["reset_in_s"] - CFG.margin + LATE < result["end"]]
    assert len(drained) >= len(due) > 0
