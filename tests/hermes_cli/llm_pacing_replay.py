"""Replay of the LLM pacing capacity model over a week of SYNTHETIC token demand (phase-1 acceptance D2). [daveey]

``python -m tests.hermes_cli.llm_pacing_replay`` rewrites ``fixtures/llm_pacing_replay_synthetic.json`` from
:func:`generate`; :func:`replay` runs the pure model (``kanban_llm_pacing``) over it.

The fixture is shaped like the hub's 11-12..11-19 week (brief section B), never copied from it: per-model
``tokens_io`` demand in 15-minute buckets (a diurnal curve with deterministic noise, Claude ~1.6x and GPT ~1.3x the
pools' weekly reserve capacity), and accounts starting like the 11-19 07:35Z read: Anthropic 8 accounts (6 at
100% weekly with resets spread over three days, 2 under reserve, five-hour windows idle), Codex 10 weekly-only
accounts (8 at 100% resetting after the week), xAI one account at 32%. Account ids are synthetic.

Each 300 s read the replay reports the accounts' rounded used percent (integers, as the endpoints do), measures
tokens per percent from the spend the throttle let through (starting unknown: the pool passes until measured),
plans, and spends ``min(demand, planned rate)`` split over the accounts by their planned rates."""
from __future__ import annotations

import json
import math
from pathlib import Path

from hermes_cli import kanban_llm_pacing as lp
from hermes_cli.machine_throughput_metrics import provider_for_model

FIXTURE = Path(__file__).parent / "fixtures" / "llm_pacing_replay_synthetic.json"
START = 1_795_000_000.0
BUCKET_S = 900
READ_S = 300
WEEK = 7 * 86400
HOURS = 3600.0


def _demand(mean_per_s: float, seed: int, buckets: int) -> list[int]:
    out = []
    for i in range(buckets):
        hour = (i * BUCKET_S / 3600.0) % 24
        diurnal = 1.0 + 0.3 * math.sin((hour - 9) / 24 * 2 * math.pi)
        noise = 1.0 + 0.1 * math.sin(i * 12.9898 + seed * 78.233)  # deterministic, no RNG state
        out.append(int(mean_per_s * BUCKET_S * diurnal * noise))
    return out


def generate() -> dict:
    buckets = WEEK // BUCKET_S
    tpp = {"anthropic": 2.6e5, "openai-codex": 1.1e6, "xai-oauth": 1.5e6}
    anthropic = [{"id": f"anthropic-{n}", "used_7d": 100, "reset_in_s": int((0.5 + n * 0.55) * 86400),
                  "used_5h": 0} for n in range(6)]
    anthropic += [{"id": "anthropic-6", "used_7d": 42, "reset_in_s": int(3.2 * 86400), "used_5h": 0},
                  {"id": "anthropic-7", "used_7d": 60, "reset_in_s": int(5.1 * 86400), "used_5h": 0}]
    codex = [{"id": f"codex-{n}", "used_7d": 100, "reset_in_s": int((5.2 + n * 0.08) * 86400)} for n in range(8)]
    codex += [{"id": "codex-8", "used_7d": 30, "reset_in_s": int(2.5 * 86400)},
              {"id": "codex-9", "used_7d": 55, "reset_in_s": int(4.4 * 86400)}]
    xai = [{"id": "xai-0", "used_7d": 32, "reset_in_s": int(4.6 * 86400)}]

    def week_rate(provider: str, n: int, factor: float) -> float:
        return factor * n * 85 * tpp[provider] / WEEK

    return {
        "start": START, "bucket_s": BUCKET_S,
        "tokens_io": {  # per model, the series' ``tokens_io:<model>`` shape
            "claude-opus-4-7": _demand(0.7 * week_rate("anthropic", 8, 1.6), 1, buckets),
            "claude-sonnet-4-6": _demand(0.3 * week_rate("anthropic", 8, 1.6), 2, buckets),
            "gpt-6-astra": _demand(week_rate("openai-codex", 10, 1.3), 3, buckets),
            "grok-5": _demand(week_rate("xai-oauth", 1, 2.0), 4, buckets),
        },
        "pools": {
            "anthropic": {"tpp_7d": tpp["anthropic"], "tpp_5h": tpp["anthropic"] / 6, "accounts": anthropic},
            "openai-codex": {"tpp_7d": tpp["openai-codex"], "tpp_5h": None, "accounts": codex},
            "xai-oauth": {"tpp_7d": tpp["xai-oauth"], "tpp_5h": None, "accounts": xai},
        },
    }


class _Account:
    def __init__(self, spec: dict, start: float, has_5h: bool):
        self.id = lp.account_id(spec["id"])
        self.used7, self.reset7 = float(spec["used_7d"]), start + spec["reset_in_s"]
        self.used5 = 0.0 if has_5h else None
        self.reset5 = None
        self.planned = False  # this weekly window has been planned (it was under reserve at a read)
        self.drains: list[tuple[float, float]] = []  # (read whose rounded weekly reading reached reserve, its reset)

    def roll(self, now: float) -> None:
        if now >= self.reset7:
            self.used7, self.reset7, self.planned = 0.0, self.reset7 + WEEK, False
        if self.reset5 is not None and now >= self.reset5:
            self.used5, self.reset5 = 0.0, None

    def readings(self, now: float) -> list[lp.Reading]:
        out = [lp.Reading(now, self.id, lp.SEVEN_DAY, float(round(self.used7)), self.reset7)]
        if self.used5 is not None:
            out.append(lp.Reading(now, self.id, lp.FIVE_HOUR, float(round(self.used5)), self.reset5))
        return out

    def spend(self, tokens: float, now: float, tpp7: float, tpp5: float | None) -> None:
        self.used7 = min(100.0, self.used7 + tokens / tpp7)
        if self.used5 is not None and tokens > 0:
            self.reset5 = self.reset5 or now + 5 * HOURS
            self.used5 = min(100.0, self.used5 + tokens / tpp5)


def _demand_at(fixture: dict, provider: str, now: float) -> float:
    """Tokens the fleet asks of ``provider`` in the read interval starting at ``now`` (by model prefix)."""
    index = int((now - fixture["start"]) // fixture["bucket_s"])
    per_read = READ_S / fixture["bucket_s"]
    return sum(series[index] * per_read for model, series in fixture["tokens_io"].items()
               if provider_for_model(model) == provider and index < len(series))


def replay(fixture: dict, provider: str, cfg: lp.PacingConfig = lp.PacingConfig()) -> dict:
    """Replay one pool over the fixture's week; returns per-read projections and per-account drain times."""
    spec = fixture["pools"][provider]
    start = fixture["start"]
    tpp7_true, tpp5_true = spec["tpp_7d"], spec["tpp_5h"]
    accounts = [_Account(a, start, tpp5_true is not None) for a in spec["accounts"]]
    samples: list[lp.Reading] = []
    tokens: list[tuple[float, float]] = []
    marks: list[tuple[str, float]] = []
    reads: list[set[str]] = []
    previous: list[lp.Reading] = []
    tpp = {lp.SEVEN_DAY: lp.Tpp(None), lp.FIVE_HOUR: lp.Tpp(None)}
    cumulative = 0.0
    projections, events = [], []
    steps = int(len(next(iter(fixture["tokens_io"].values()))) * fixture["bucket_s"] // READ_S)
    for step in range(steps):
        now = start + step * READ_S
        for account in accounts:
            account.roll(now)
        current = [r for a in accounts for r in a.readings(now)]
        found = lp.detect_events(provider, current, previous, reads[1:], set().union(*reads[:288]) if reads else
                                 {r.account for r in current}, now) if previous else []
        events += found
        marks += [(e["account"], now) for e in found]
        samples = [s for s in samples if s.ts >= now - cfg.tpp_window] + current
        tokens = [t for t in tokens if t[0] >= now - cfg.tpp_window] + [(now, cumulative)]
        for kind, previous_tpp in list(tpp.items()):
            estimate = lp.tpp_estimate(samples, tokens, kind, now, cfg, marks)
            tpp[kind] = lp.smooth_tpp(previous_tpp, estimate, now, cfg, lp.Tpp(None))
        pool, _ = lp.plan_pool(provider, current, tpp[lp.SEVEN_DAY], tpp[lp.FIVE_HOUR], now, cfg)
        plans = {a.id: lp.plan_account(a.id, [r for r in current if r.account == a.id], tpp[lp.SEVEN_DAY],
                                       tpp[lp.FIVE_HOUR], now, cfg) for a in accounts}
        for account in accounts:
            if plans[account.id].under_reserve:
                account.planned = True
            elif account.planned and round(account.used7) >= cfg.reserve[lp.SEVEN_DAY]:
                account.drains.append((now, account.reset7))
                account.planned = False
        projections.append({"now": now, "exhaust_plan_at": pool["exhaust_plan_at"], "rate": pool["rate_tokens_per_s"],
                            "tpp_source": pool["tpp_source"], "accounts": {
                                a.id: (plans[a.id].remaining, plans[a.id].rate, a.reset7) for a in accounts}})
        demand = _demand_at(fixture, provider, now)
        open_accounts = [a for a in accounts if plans[a.id].under_reserve]
        if pool["rate_tokens_per_s"] is None:  # unknown exchange rate: the throttle passes the pool
            shares = {a.id: demand / len(open_accounts) for a in open_accounts} if open_accounts else {}
        else:
            allowed = min(demand, pool["rate_tokens_per_s"] * READ_S)
            total = pool["rate_tokens_per_s"] or 1.0
            shares = {a.id: allowed * (plans[a.id].rate or 0.0) / total for a in open_accounts}
        for account in accounts:
            account.spend(shares.get(account.id, 0.0), now, tpp7_true, tpp5_true)
        cumulative += sum(shares.values())
        reads.insert(0, {r.account for r in current})
        previous = current
    return {"projections": projections, "events": events,
            "drains": {a.id: a.drains for a in accounts}, "tpp": tpp, "end": start + steps * READ_S}


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(generate(), separators=(",", ":")) + "\n", encoding="utf-8")
    print(FIXTURE)
