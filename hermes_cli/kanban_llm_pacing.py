"""LLM quota pacing for Kanban dispatch: the pool math (pure; IO lives in ``kanban_llm_pacing_dispatch``). [daveey]

2026-11-13/14: the fleet (hub + 8 workers) spawned deep-class work at ~2.7x the Claude pool's sustainable
weekly rate. One account-week went in under 21 h, 6 of 7 accounts sat at ``seven_day`` 100%, Codex was walled
for a week, and nothing finished for hours (301 refused starts). Admission was gated on slots and walls only.

The governor judges each governed provider's OAuth pool from its per-account usage windows (``five_hour`` and
``seven_day``, the account-wide buckets of ``/api/oauth/usage``; Codex ``Session``/``Weekly``; xAI Grok's single
weekly pool, ``agent.account_usage_xai``):

* **Projection, not elapsed pace.** Each window's burn rate is measured from this host's own samples of that
  window over the last ``rate_window`` (fallback: the window's average so far). A used-vs-elapsed rule read
  "behind pace" ten minutes before an account hit 100% (askevepodcast 11-14 14:29Z: 92% used, 94% elapsed,
  +0.8%/min). The pool factor is ``sum(sustainable rate of headroom accounts) / sum(measured burn)`` per
  window kind, where a sustainable rate reaches ``target`` (95%) exactly at that window's reset. Factor < 1
  means the pool, at today's burn, ends above target: ``paced``.
* **Reserve floor.** An account past ``reserve`` (five_hour 80% / seven_day 85%) is left to chats, cron and
  notices. When every readable account is past it, the pool is ``held`` until the earliest reset that frees
  one, so Kanban never drains the last headroom the human-facing callers need.
* **Fail open.** No readable account (read failure, empty pool, endpoint down) is ``unknown``: today's behaviour.

The legacy ``pace`` verdict above is still judged and stored for one release (hermes-mods ``throughput/runway.py``
reads it), but it no longer limits starts: the per-host start limit (``pace_starts``, ``deferrable_share``) is gone
(2026-11-17: with both pools ``paced`` every host was cut to one run per provider, 3 of 18 slots busy and 130 cards
waiting). A start waits only while its pool is **at reserve** (no account under reserve).

**Capacity model (``llm_pacing.json`` ``v2``).** The token rate each pool can sustain until its accounts reach
their reserve ``margin`` seconds before their resets, for a token throttle to enforce (the hermes-mods throughput
plugin publishes it; core never throttles):

* An **exchange rate** (tokens per percent, ``tpp``) per pool and window kind, from consecutive reads: the fleet's
  provider token delta over the sum of the accounts' percent deltas (:func:`tpp_estimate`, smoothed by
  :func:`smooth_tpp`), else a configured seed, else ``unknown``. One rate per pool: accounts in a pool are assumed to
  be on the same plan size (``plan_size_assumed_equal``), because tokens cannot be attributed to accounts.
* **Remaining** per account: ``max(0, reserve - used) x tpp``; a window with no reset counts as a fresh full one.
* **Rate** per account: weekly remaining over the time to ``reset - margin`` (at least 600 s), capped by its five-hour
  remaining over the time to that reset (at least 60 s); the pool rate is their sum over accounts under reserve.
  Unknown tpp = ``null`` rate (a throttle passes the pool: fail open), reserve still holds.
* **Events** between reads (:func:`detect_events`): accounts added or removed, scheduled and early resets.

Anthropic's model-scoped weekly windows (``seven_day_opus``/``seven_day_sonnet``) are recorded in the history only;
they never change an account's capacity. Account labels are e-mail addresses: the model only sees
:func:`account_id` hashes.
"""
from __future__ import annotations

import bisect
import hashlib
import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

FIVE_HOUR, SEVEN_DAY = "five_hour", "seven_day"
WINDOW_SECONDS = {FIVE_HOUR: 5 * 3600, SEVEN_DAY: 7 * 86400}
OPEN, PACED, HELD, UNKNOWN = "open", "paced", "held", "unknown"
GUARD_REASON = "llm_pacing"
# A sample belongs to the same window instance when its reset is within this of the current reading's.
_SAME_RESET_SECONDS = 300
MODEL_KINDS = ("seven_day_opus", "seven_day_sonnet")  # model-scoped windows: history only, never capacity


@dataclass(frozen=True)
class PacingConfig:
    enabled: bool = True
    target: float = 95.0
    reserve: dict = field(default_factory=lambda: {FIVE_HOUR: 80.0, SEVEN_DAY: 85.0})
    providers: tuple = ("anthropic", "openai-codex", "xai-oauth")
    read_interval: int = 300
    rate_window: int = 3600
    min_rate_span: int = 600
    margin: int = 10800  # plan each account to its reserve this many seconds before its weekly reset
    history_days: int = 8
    tpp_min_delta: float = 5.0  # percent: below this summed delta an exchange-rate estimate means nothing
    tpp_window: int = 86400
    tpp_half_life: int = 43200
    tokens_per_percent: dict = field(default_factory=dict)  # {provider: {kind: tokens per percent}} seeds
    budget_publish: bool = False  # read by the throughput plugin: this host publishes the budget to the throttle


def _num(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def config_from(section: Any) -> PacingConfig:
    """``kanban.llm_pacing`` -> :class:`PacingConfig`; anything unreadable keeps the default."""
    base = PacingConfig()
    raw = section if isinstance(section, dict) else {}
    providers = raw.get("providers", base.providers)
    providers = tuple(str(p).strip().lower() for p in providers) if isinstance(providers, (list, tuple)) else base.providers
    return PacingConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        target=_num(raw.get("target_percent"), base.target),
        reserve={FIVE_HOUR: _num(raw.get("reserve_five_hour_percent"), base.reserve[FIVE_HOUR]),
                 SEVEN_DAY: _num(raw.get("reserve_seven_day_percent"), base.reserve[SEVEN_DAY])},
        providers=providers,
        read_interval=int(_num(raw.get("read_interval_seconds"), base.read_interval)),
        rate_window=int(_num(raw.get("rate_window_seconds"), base.rate_window)),
        min_rate_span=int(_num(raw.get("min_rate_span_seconds"), base.min_rate_span)),
        margin=max(0, int(_num(raw.get("margin_seconds"), base.margin))),
        history_days=max(1, int(_num(raw.get("history_days"), base.history_days))),
        tpp_min_delta=max(0.0, _num(raw.get("tpp_min_delta_percent"), base.tpp_min_delta)),
        tpp_window=max(1, int(_num(raw.get("tpp_window_seconds"), base.tpp_window))),
        tpp_half_life=max(1, int(_num(raw.get("tpp_half_life_seconds"), base.tpp_half_life))),
        tokens_per_percent=_seeds(raw.get("tokens_per_percent")),
        budget_publish=raw.get("budget_publish", base.budget_publish) is True,
    )


_KIND_ALIASES = {"seven_day": SEVEN_DAY, "7d": SEVEN_DAY, "week": SEVEN_DAY,
                 "five_hour": FIVE_HOUR, "5h": FIVE_HOUR, "session": FIVE_HOUR}


def _seeds(raw: Any) -> dict:
    """``tokens_per_percent.<provider>.<kind>`` -> ``{provider: {kind: tpp}}``; non-positive or unreadable dropped."""
    out: dict = {}
    for provider, kinds in (raw.items() if isinstance(raw, dict) else ()):
        for kind, value in (kinds.items() if isinstance(kinds, dict) else ()):
            name, number = _KIND_ALIASES.get(str(kind).strip().lower()), _num(value, 0.0)
            if name and number > 0:
                out.setdefault(str(provider).strip().lower(), {})[name] = number
    return out


@dataclass(frozen=True)
class Reading:
    """One account's usage window at one moment (``ts``): ``used`` percent, ``reset_at`` epoch seconds."""
    ts: float
    account: str
    kind: str
    used: float
    reset_at: Optional[float]

    def as_row(self) -> list:
        return [self.ts, self.account, self.kind, self.used, self.reset_at]

    @classmethod
    def from_row(cls, row: Any) -> Optional[Reading]:
        try:
            ts, account, kind, used, reset_at = row
            return cls(float(ts), str(account), str(kind), float(used),
                       None if reset_at is None else float(reset_at))
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class PoolPace:
    provider: str
    state: str
    factor: Optional[float] = None
    until: Optional[float] = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "state": self.state, "factor": self.factor, "until": self.until,
                "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Any) -> Optional[PoolPace]:
        if not isinstance(data, dict) or data.get("state") not in (OPEN, PACED, HELD, UNKNOWN):
            return None
        return cls(str(data.get("provider") or ""), data["state"], data.get("factor"), data.get("until"),
                   str(data.get("detail") or ""))


def window_kind(label: Any) -> str:
    """``Current week``/``Weekly`` -> seven_day; ``Current session``/``Session`` -> five_hour."""
    return SEVEN_DAY if "week" in str(label or "").lower() else FIVE_HOUR


def burn_rate(reading: Reading, history: Iterable[Reading], cfg: PacingConfig) -> float:
    """Percent per second this window is burning: the steeper of the slopes from the oldest and the newest
    same-window sample inside ``rate_window`` (at least ``min_rate_span`` old), so a fresh burst counts at once;
    else the window's average since it opened. A capped window (100%) burns nothing more."""
    now = reading.ts
    same = [h for h in history
            if h.account == reading.account and h.kind == reading.kind
            and now - cfg.rate_window <= h.ts <= now - cfg.min_rate_span
            and (h.reset_at is None) == (reading.reset_at is None)
            and abs((h.reset_at or 0) - (reading.reset_at or 0)) <= _SAME_RESET_SECONDS]
    if same:
        ends = (min(same, key=lambda h: h.ts), max(same, key=lambda h: h.ts))
        return max(0.0, *((reading.used - h.used) / max(1.0, now - h.ts) for h in ends))
    if reading.reset_at is None or reading.used >= 100:
        return 0.0
    elapsed = WINDOW_SECONDS[reading.kind] - (reading.reset_at - now)
    return max(0.0, reading.used / max(float(cfg.min_rate_span), elapsed))


def _reserved(reading: Reading, cfg: PacingConfig) -> bool:
    return reading.used >= cfg.reserve.get(reading.kind, 100.0)


def _held_until(current: list[Reading], cfg: PacingConfig) -> Optional[float]:
    """Earliest moment one reserved account frees: per account, the latest reset among its reserved windows."""
    frees = []
    for account in {r.account for r in current}:
        reserved = [r for r in current if r.account == account and _reserved(r, cfg)]
        resets = [r.reset_at for r in reserved if r.reset_at is not None]
        if reserved and len(resets) == len(reserved):
            frees.append(max(resets))
    return min(frees) if frees else None


def _sustainable(reading: Optional[Reading], kind: str, cfg: PacingConfig) -> float:
    """Percent per second a headroom account can burn in ``kind`` and reach ``target`` at its reset. A window not
    started yet counts as a fresh full window: ``/api/oauth/usage`` reports an idle account's five_hour as
    ``null`` (no reading at all; 6 of 7 accounts on 11-14 16:57Z), or a reading may carry no reset."""
    used = reading.used if reading is not None else 0.0
    if reading is None or reading.reset_at is None:
        return max(0.0, cfg.target - used) / WINDOW_SECONDS[kind]
    return max(0.0, cfg.target - used) / max(60.0, reading.reset_at - reading.ts)


def _factor(current: list[Reading], history: list[Reading], headroom: set[str], cfg: PacingConfig) -> float:
    factor = math.inf
    for kind in (FIVE_HOUR, SEVEN_DAY):
        readings = [r for r in current if r.kind == kind]
        burn = sum(burn_rate(r, history, cfg) for r in readings)
        by_account = {r.account: r for r in readings}
        sustainable = sum(_sustainable(by_account.get(account), kind, cfg) for account in headroom)
        if burn > 0:
            factor = min(factor, sustainable / burn)
    return factor


def _describe(current: list[Reading], history: list[Reading], cfg: PacingConfig) -> str:
    parts = []
    for r in sorted(current, key=lambda r: (r.account, r.kind)):
        rate = burn_rate(r, history, cfg) * 60
        parts.append(f"{r.account} {'5h' if r.kind == FIVE_HOUR else '7d'} {r.used:.0f}% +{rate:.2f}%/min")
    return "; ".join(parts)


def pool_pace(provider: str, current: list[Reading], history: list[Reading], cfg: PacingConfig) -> PoolPace:
    """Judge one provider's pool from this read (``current``) and earlier samples (``history``)."""
    if not current:
        return PoolPace(provider, UNKNOWN, detail="no readable account")
    detail = _describe(current, history, cfg)
    headroom = {r.account for r in current} - {r.account for r in current if _reserved(r, cfg)}
    if not headroom:
        return PoolPace(provider, HELD, 0.0, _held_until(current, cfg), detail)
    factor = _factor(current, history, headroom, cfg)
    if factor >= 1.0:
        return PoolPace(provider, OPEN, None if math.isinf(factor) else round(factor, 4), None, detail)
    return PoolPace(provider, PACED, round(factor, 4), None, detail)


# ---- capacity model (``llm_pacing.json`` v2) ----

V2_VERSION = 2
MEASURED, SEED, UNKNOWN_TPP = "measured", "seed", "unknown"
ACCOUNT_ADDED, ACCOUNT_REMOVED, RESET, RESET_EARLY = "account_added", "account_removed", "reset", "reset_early"
GOVERNED_KINDS = (FIVE_HOUR, SEVEN_DAY)
MIN_PLAN_SECONDS = {SEVEN_DAY: 600.0, FIVE_HOUR: 60.0}
TPP_MIN_SPAN_SECONDS = 7200
ADDED_UNSEEN_SECONDS = 86400
REMOVED_AFTER_READS = 3
_RESET_JUMP_SECONDS = 300
_EARLY_DROP_POINTS = 10.0
RESET_SLACK_SECONDS = 86400  # a reset further out than its window plus this is clamped (bad or hostile input)
MAX_CURVE_POINTS = 64


def valid_readings(readings: Iterable[Reading]) -> list[Reading]:
    """Readings fit to store and plan: a non-finite ``used`` reads as exhausted (100%: reserved for the ``held``
    verdict, no capacity for the plan), never as headroom, and never reaches JSON as NaN/inf; a non-finite
    ``reset_at`` reads as no reset."""
    out = []
    for r in readings:
        used = r.used if math.isfinite(r.used) else 100.0
        reset = r.reset_at if r.reset_at is not None and math.isfinite(r.reset_at) else None
        out.append(r if (used, reset) == (r.used, r.reset_at) else Reading(r.ts, r.account, r.kind, used, reset))
    return out


def _clamped(r: Reading, now: float) -> Reading:
    """``r`` with its reset clamped into ``[now, now + window + slack]``, so planning cost and the curve never
    depend on how far away (or how far past) a reset claims to be (a year-9999 reset once hung the claim path)."""
    if r.reset_at is None:
        return r
    top = now + WINDOW_SECONDS.get(r.kind, WINDOW_SECONDS[SEVEN_DAY]) + RESET_SLACK_SECONDS
    reset = min(top, max(now, r.reset_at))
    return r if reset == r.reset_at else Reading(r.ts, r.account, r.kind, r.used, reset)


def account_id(label: Any) -> str:
    """The id an account goes by in the history and ``v2``: never its label (an e-mail address)."""
    return hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:16]


def hashed(readings: Iterable[Reading]) -> list[Reading]:
    return [Reading(r.ts, account_id(r.account), r.kind, r.used, r.reset_at) for r in readings]


@dataclass(frozen=True)
class Tpp:
    """Tokens per percent of one pool's window kind, how it was obtained and when it was last measured."""
    value: Optional[float]
    source: str = UNKNOWN_TPP
    at: Optional[float] = None


def seed_tpp(provider: str, kind: str, cfg: PacingConfig) -> Tpp:
    value = (cfg.tokens_per_percent.get(provider) or {}).get(kind)
    return Tpp(float(value), SEED) if value else Tpp(None, UNKNOWN_TPP)


def _by_ts(samples: Iterable[Reading], kind: str) -> dict[float, dict[str, Reading]]:
    reads: dict[float, dict[str, Reading]] = {}
    for r in samples:
        if r.kind == kind:
            reads.setdefault(r.ts, {})[r.account] = r
    return reads


def _interval_delta(before: dict[str, Reading], after: dict[str, Reading], t0: float, t1: float,
                    marks: dict[str, list[float]]) -> Optional[float]:
    """Summed percent spent between two reads, or None when an account's window reset (or an event touched it)
    inside the interval: its tokens then bought percent this delta cannot see, so the whole interval is void."""
    delta = 0.0
    for account, r0 in before.items():
        r1 = after.get(account)
        if r1 is None or r0.used >= 100:
            continue
        stamps = marks.get(account)
        if stamps and bisect.bisect_right(stamps, t1) > bisect.bisect_right(stamps, t0):
            return None
        if r1.used < r0.used or (r0.reset_at is not None and r1.reset_at is not None
                                 and abs(r1.reset_at - r0.reset_at) > _RESET_JUMP_SECONDS):
            return None
        delta += r1.used - r0.used
    return delta


def tpp_estimate(samples: Iterable[Reading], tokens: Iterable[tuple[float, float]], kind: str, now: float,
                 cfg: PacingConfig, marks: Iterable[tuple[str, float]] = ()) -> Optional[float]:
    """Tokens per percent of ``kind`` over the last ``tpp_window``: the provider's cumulative-token delta over the
    accounts' summed percent delta, across consecutive successful reads (both with a token reading). An interval
    over a reset or an event (``marks``: ``(account, ts)``) is skipped, and an account starting at 100% adds nothing.
    None unless the summed delta reaches ``tpp_min_delta`` over at least two hours."""
    since = now - cfg.tpp_window
    reads = {ts: accounts for ts, accounts in _by_ts(samples, kind).items() if since <= ts <= now}
    cumulative = {float(ts): float(value) for ts, value in tokens}
    marked: dict[str, list[float]] = {}
    for account, ts in marks:
        marked.setdefault(account, []).append(float(ts))
    for stamps in marked.values():
        stamps.sort()
    times = sorted(ts for ts in reads if ts in cumulative)
    percent = spent = 0.0
    first = last = None
    for t0, t1 in itertools.pairwise(times):
        delta = _interval_delta(reads[t0], reads[t1], t0, t1, marked)
        used_tokens = cumulative[t1] - cumulative[t0]
        if delta is None or used_tokens < 0:
            continue
        percent, spent = percent + delta, spent + used_tokens
        first, last = (t0 if first is None else first), t1
    if first is None or percent < max(cfg.tpp_min_delta, 1e-9) or last - first < TPP_MIN_SPAN_SECONDS or spent <= 0:
        return None
    return spent / percent


def smooth_tpp(previous: Optional[Tpp], estimate: Optional[float], now: float, cfg: PacingConfig,
               seed: Tpp) -> Tpp:
    """EWMA (``tpp_half_life``) of the estimates, each update clamped to 0.5x-2x the previous value; the last
    measured value persists while no estimate is valid; never measured = the seed (or unknown)."""
    measured = previous if previous is not None and previous.source == MEASURED and previous.value else None
    if estimate is None or estimate <= 0:
        return measured or seed
    if measured is None:
        return Tpp(estimate, MEASURED, now)
    alpha = 1.0 - 0.5 ** (max(0.0, now - float(measured.at or now)) / cfg.tpp_half_life)
    value = measured.value + alpha * (estimate - measured.value)
    return Tpp(min(2 * measured.value, max(0.5 * measured.value, value)), MEASURED, now)


def _reset_event(r0: Reading, r1: Reading, now: float) -> Optional[str]:
    """``reset`` / ``reset_early`` for one account window between two reads, else None. Once the prior reset time
    has come (within 300 s), a drop or a later reset is the scheduled reset; before it, a drop of 10 points (or to
    at most 1%) or a reset moved by more than 300 s is an early one."""
    if r0.reset_at is None:
        return None  # a window that had not started: a first reset is it starting, not resetting
    dropped = r0.used - r1.used >= _EARLY_DROP_POINTS or (r1.used <= 1 < r0.used)
    moved = r1.reset_at is not None and abs(r1.reset_at - r0.reset_at) > _RESET_JUMP_SECONDS
    if now >= r0.reset_at - _RESET_JUMP_SECONDS:
        return RESET if dropped or moved or r1.reset_at is None else None
    return RESET_EARLY if dropped or moved else None


def detect_events(provider: str, current: list[Reading], previous: list[Reading], earlier: list[set[str]],
                  seen_recently: set[str], now: float) -> list[dict]:
    """Events between the last successful read (``previous``) and this one (``current``), on hashed accounts.
    ``earlier``: the account sets of the successful reads before ``previous`` (newest first); ``seen_recently``:
    the accounts read in the last day before this read."""
    events: list[dict] = []

    def add(account: str, kind: str, detail: dict) -> None:
        events.append({"ts": now, "provider": provider, "account": account, "type": kind, "detail": detail})

    accounts = {r.account for r in current}
    for account in sorted(accounts - seen_recently):
        add(account, ACCOUNT_ADDED, {})
    reads = [{r.account for r in previous}, *earlier]  # newest first, before this one
    if len(reads) >= REMOVED_AFTER_READS:  # absent now and in the 2 reads before, present in the one before those
        gone = reads[REMOVED_AFTER_READS - 1] - accounts - set().union(*reads[:REMOVED_AFTER_READS - 1])
        for account in sorted(gone):
            add(account, ACCOUNT_REMOVED, {"absent_reads": REMOVED_AFTER_READS})
    before = {(r.account, r.kind): r for r in previous}
    for r1 in current:
        r0 = before.get((r1.account, r1.kind))
        kind = _reset_event(r0, r1, now) if r0 is not None else None
        if kind:
            add(r1.account, kind, {"kind": r1.kind, "used_before": r0.used, "used": r1.used,
                                   "reset_before": r0.reset_at, "reset_at": r1.reset_at})
    return events


@dataclass(frozen=True)
class AccountPlan:
    id: str
    under_reserve: bool
    remaining: Optional[float]  # weekly tokens left to the reserve (None: tpp unknown)
    reset_at: float  # the weekly reset (a window not started counts as a fresh full one)
    rate: Optional[float]  # tokens/s the plan spends on it (0 when not under reserve)
    rate_cap_5h: Optional[float]  # its five-hour cap; None = no five-hour window (Codex, xAI) or tpp unknown


def _window(readings: list[Reading], kind: str) -> Optional[Reading]:
    return next((r for r in readings if r.kind == kind), None)


def _cap_5h(window: Optional[Reading], tpp: Tpp, now: float, cfg: PacingConfig) -> Optional[float]:
    if window is None or not tpp.value:
        return None
    reserve = cfg.reserve[FIVE_HOUR]
    if window.reset_at is None:
        return reserve * tpp.value / WINDOW_SECONDS[FIVE_HOUR]
    return max(0.0, reserve - window.used) * tpp.value / max(MIN_PLAN_SECONDS[FIVE_HOUR], window.reset_at - now)


def plan_account(account: str, readings: list[Reading], tpp7: Tpp, tpp5: Tpp, now: float,
                 cfg: PacingConfig) -> AccountPlan:
    week = _window(readings, SEVEN_DAY)
    used7 = week.used if week is not None else 0.0
    reset7 = week.reset_at if week is not None and week.reset_at is not None else now + WINDOW_SECONDS[SEVEN_DAY]
    under = not any(_reserved(r, cfg) for r in readings if r.kind in GOVERNED_KINDS)
    remaining = max(0.0, cfg.reserve[SEVEN_DAY] - used7) * tpp7.value if tpp7.value else None
    cap5 = _cap_5h(_window(readings, FIVE_HOUR), tpp5, now, cfg)
    if remaining is None:
        rate = None
    elif not under:
        rate = 0.0
    else:
        rate = remaining / max(MIN_PLAN_SECONDS[SEVEN_DAY], reset7 - cfg.margin - now)
        rate = rate if cap5 is None else min(rate, cap5)
    return AccountPlan(account, under, remaining, reset7, rate, cap5)


def _exhaust_at(plan: AccountPlan, now: float) -> Optional[float]:
    if plan.remaining is None or not plan.rate:
        return None
    return now + plan.remaining / plan.rate


def _curve(plans: list[AccountPlan], tpp7: Tpp, now: float, cfg: PacingConfig) -> list[list[float]]:
    """``[[t, capacity, planned_spend]]``: cumulative weekly capacity from now (the remaining, plus a step of
    ``reserve x tpp`` at every reset, repeating each week out to the furthest reset) and the current plan's
    cumulative spend of the remaining. Empty while tpp is unknown."""
    if not tpp7.value or not plans:
        return []
    step = cfg.reserve[SEVEN_DAY] * tpp7.value
    week = WINDOW_SECONDS[SEVEN_DAY]
    horizon = max(p.reset_at for p in plans)  # resets are clamped (``_clamped``): at most window + slack away
    # Each reset repeats weekly out to the horizon: with clamped resets that is at most two steps per account.
    resets = sorted(p.reset_at + k * week for p in plans for k in range(int((horizon - p.reset_at) // week) + 1))
    times = sorted({now, horizon, *resets, *(e for p in plans if (e := _exhaust_at(p, now)) is not None)})
    if len(times) > MAX_CURVE_POINTS:  # keep the ends, thin the middle evenly
        stride = (len(times) - 1) / (MAX_CURVE_POINTS - 1)
        times = sorted({times[round(i * stride)] for i in range(MAX_CURVE_POINTS)})
    base = sum(p.remaining or 0.0 for p in plans)
    out = []
    for t in times:
        capacity = base + step * bisect.bisect_right(resets, t)
        spend = sum(min(p.remaining or 0.0, (p.rate or 0.0) * (t - now)) for p in plans)
        out.append([round(t, 1), round(capacity, 1), round(spend, 1)])
    return out


def plan_pool(provider: str, current: list[Reading], tpp7: Tpp, tpp5: Tpp, now: float,
              cfg: PacingConfig) -> tuple[dict, list[list[float]]]:
    """One pool's ``v2.pools`` entry (without ``ratio``) and its curve, from this read's hashed account windows.
    ``at_reserve``: at least one account read and none under reserve (the pool then plans rate 0); no readable
    account = not at reserve and rate ``null`` (fail open). ``until``: the earliest reset that frees an account."""
    governed = [_clamped(r, now) for r in valid_readings(current) if r.kind in GOVERNED_KINDS]
    by_account: dict[str, list[Reading]] = {}
    for r in governed:
        by_account.setdefault(r.account, []).append(r)
    plans = [plan_account(a, rs, tpp7, tpp5, now, cfg) for a, rs in sorted(by_account.items())]
    at_reserve = bool(plans) and not any(p.under_reserve for p in plans)
    known = bool(plans) and tpp7.value is not None
    rate = 0.0 if at_reserve else (sum(p.rate or 0.0 for p in plans) if known else None)
    caps = [p.rate_cap_5h for p in plans if p.under_reserve and p.rate_cap_5h is not None]
    exhaust = [e for p in plans if (e := _exhaust_at(p, now)) is not None]
    pool = {
        "read_at": now,
        "accounts": [{"id": r.account, "kind": r.kind, "used": r.used, "reset_at": r.reset_at,
                      "tpp": (tpp7 if r.kind == SEVEN_DAY else tpp5).value}
                     for r in sorted(governed, key=lambda r: (r.account, r.kind))],
        "tpp_7d": tpp7.value, "tpp_5h": tpp5.value, "tpp_source": tpp7.source, "tpp_5h_source": tpp5.source,
        "tpp_measured_at": tpp7.at, "tpp_5h_measured_at": tpp5.at,
        "plan_size_assumed_equal": True,
        "remaining_tokens": sum(p.remaining or 0.0 for p in plans) if known else None,
        "exhaust_plan_at": max(exhaust) if exhaust else None,
        "rate_tokens_per_s": rate,
        "rate_cap_5h": sum(caps) if caps else None,
        "at_reserve": at_reserve,
        "until": _held_until(governed, cfg) if at_reserve else None,
    }
    return pool, _curve(plans, tpp7, now, cfg)


def reserve_pool(current: list[Reading], now: float, cfg: PacingConfig) -> dict:
    """The least ``v2`` pool entry: ``at_reserve`` and ``until`` alone, from the same rule as the legacy ``held``
    verdict. The fallback when the full plan fails, so a v2 fault never changes what holds a start."""
    governed = [_clamped(r, now) for r in valid_readings(current) if r.kind in GOVERNED_KINDS]
    at_reserve = bool(governed) and not ({r.account for r in governed} - {r.account for r in governed
                                                                          if _reserved(r, cfg)})
    return {"read_at": now, "accounts": [], "at_reserve": at_reserve, "rate_tokens_per_s": 0.0 if at_reserve else None,
            "until": _held_until(governed, cfg) if at_reserve else None, "plan_size_assumed_equal": True}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def stored_tpp(pool: Any, kind: str) -> Optional[Tpp]:
    """The exchange rate a previous ``v2`` pool entry carried for ``kind`` (the persisted last good value)."""
    if not isinstance(pool, dict):
        return None
    suffix = "7d" if kind == SEVEN_DAY else "5h"
    value = pool.get(f"tpp_{suffix}")
    source = pool.get("tpp_source" if kind == SEVEN_DAY else "tpp_5h_source")
    at = pool.get("tpp_measured_at" if kind == SEVEN_DAY else "tpp_5h_measured_at")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return None
    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else None
    return Tpp(float(value), source if source in (MEASURED, SEED) else UNKNOWN_TPP, at)


def assemble_v2(previous: Any, provider: str, pool: dict, curve: list, events: list[dict], now: float,
                cfg: PacingConfig) -> dict:
    """The new ``v2`` document: ``previous`` with this provider's pool and curve replaced, the global rate (pools
    whose rate is known) and every pool's ``ratio`` recomputed, the newest 50 events kept, ``computed_at`` now."""
    old = _dict(previous)  # every field read from the old file is type-checked: malformed reads as absent
    pools = {p: dict(v) for p, v in _dict(old.get("pools")).items() if p in cfg.providers and isinstance(v, dict)}
    pools[provider] = pool
    curves = {p: v for p, v in _dict(old.get("curve")).items() if p in pools and isinstance(v, list)}
    curves[provider] = curve

    def rate(v: dict) -> Optional[float]:
        r = v.get("rate_tokens_per_s")
        return float(r) if isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r) else None
    rates = [r for v in pools.values() if (r := rate(v)) is not None]
    total = sum(rates) if rates else None
    for v in pools.values():
        r = rate(v)
        v["ratio"] = (r / total if total else 0.0) if r is not None and total is not None else None
    events_old = old.get("events")
    kept = [e for e in (events_old if isinstance(events_old, list) else []) if isinstance(e, dict)] + events
    return {"computed_at": now, "margin_s": cfg.margin, "pools": pools, "global_rate_tokens_per_s": total,
            "events": kept[-50:], "curve": curves}


def route_ratios(v2: Any) -> dict[str, float]:
    """Phase 2's routing input: each governed pool's share of the global rate (0 at reserve), known pools only."""
    pools = _dict(_dict(v2).get("pools"))
    return {p: float(v["ratio"]) for p, v in pools.items()
            if isinstance(v, dict) and isinstance(v.get("ratio"), (int, float)) and not isinstance(v["ratio"], bool)
            and math.isfinite(v["ratio"])}
