"""LLM quota pacing for Kanban dispatch: the pool math (pure; IO lives in ``kanban_llm_pacing_dispatch``). [daveey]

2026-11-13/14: the fleet (hub + 8 workers) spawned deep-class work at ~2.7x the Claude pool's sustainable
weekly rate. One account-week went in under 21 h, 6 of 7 accounts sat at ``seven_day`` 100%, Codex was walled
for a week, and nothing finished for hours (301 refused starts). Admission was gated on slots and walls only.

The governor judges each governed provider's OAuth pool from its per-account usage windows (``five_hour`` and
``seven_day``, the account-wide buckets of ``/api/oauth/usage``; Codex ``Session``/``Weekly``):

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
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

FIVE_HOUR, SEVEN_DAY = "five_hour", "seven_day"
WINDOW_SECONDS = {FIVE_HOUR: 5 * 3600, SEVEN_DAY: 7 * 86400}
OPEN, PACED, HELD, UNKNOWN = "open", "paced", "held", "unknown"
GUARD_REASON = "llm_pacing"
# A sample belongs to the same window instance when its reset is within this of the current reading's.
_SAME_RESET_SECONDS = 300
# At or above this pool factor an open pool sets no limit at all; between 1 and it the host ramps.
OPEN_RAMP_FACTOR = 2.0


@dataclass(frozen=True)
class PacingConfig:
    enabled: bool = True
    target: float = 95.0
    reserve: dict = field(default_factory=lambda: {FIVE_HOUR: 80.0, SEVEN_DAY: 85.0})
    providers: tuple = ("anthropic", "openai-codex")
    read_interval: int = 300
    rate_window: int = 3600
    min_rate_span: int = 600
    deferrable_share: float = 0.5


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
        deferrable_share=min(1.0, max(0.0, _num(raw.get("deferrable_share"), base.deferrable_share))),
    )


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


def admit_limit(pace: PoolPace, avg_running: float, *, deferrable: bool, cfg: PacingConfig) -> Optional[int]:
    """Concurrent runs this host may hold on the provider (None = no limit).

    Burn is proportional to concurrency, so the host keeps ``factor`` of its recent average concurrency: every
    host does the same, and the fleet's burn lands on the sustainable rate. Deferrable work (review lane,
    negative priority, scheduled wakes) gets ``deferrable_share`` of that, so it yields first. Just behind pace
    (factor < ``OPEN_RAMP_FACTOR``) the host may grow to ``factor`` x its recent concurrency (at least one run),
    so a pool coming off a hold ramps up instead of bursting back into the wall."""
    if pace.state == UNKNOWN or (pace.state == OPEN and (pace.factor is None or pace.factor >= OPEN_RAMP_FACTOR)):
        return None
    if pace.state == HELD:
        return 0
    if pace.state == OPEN:
        return max(1, math.ceil(max(0.0, avg_running) * float(pace.factor or 1.0)))
    limit = math.floor(max(0.0, avg_running) * float(pace.factor or 0.0))
    return math.floor(limit * cfg.deferrable_share) if deferrable else limit
