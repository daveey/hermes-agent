"""Test helper: write ``llm_pacing.json`` as a dispatcher on this home leaves it (legacy ``pace`` + ``v2``). [daveey]

A pool stored ``held`` is also ``v2.pools[p].at_reserve`` (the only state that holds a start); any other state is
under reserve. Account ids are synthetic hashes, never labels."""
from __future__ import annotations

import json
import time
from typing import Optional

from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd


def v2_pool(at_reserve: bool, read_at: float, until: Optional[float] = None) -> dict:
    return {"read_at": read_at, "accounts": [{"id": lp.account_id("acct-1"), "kind": lp.SEVEN_DAY,
                                              "used": 100.0 if at_reserve else 40.0, "reset_at": until, "tpp": None}],
            "tpp_7d": None, "tpp_5h": None, "tpp_source": lp.UNKNOWN_TPP, "tpp_5h_source": lp.UNKNOWN_TPP,
            "plan_size_assumed_equal": True, "remaining_tokens": None, "exhaust_plan_at": None,
            "rate_tokens_per_s": 0.0 if at_reserve else None, "rate_cap_5h": None, "at_reserve": at_reserve,
            "until": until if at_reserve else None, "ratio": 0.0 if at_reserve else None}


def state(*pools, age: float = 0.0, now: Optional[float] = None, v2: bool = True) -> dict:
    """``(provider, state[, until])`` per pool -> the file's content."""
    now = time.time() if now is None else now
    data: dict = {}
    v2_pools = {}
    for provider, verdict, *until in pools:
        reset = until[0] if until else None
        pace = lp.PoolPace(provider, verdict, 0.0299 if verdict == lp.PACED else 0.0, reset,
                           "6 of 7 accounts at 100% on the 7-day window")
        data[provider] = {"read_at": now - age, "pace": pace.as_dict(), "samples": []}
        v2_pools[provider] = v2_pool(verdict == lp.HELD, now - age, reset)
    if v2:
        data["version"] = lp.V2_VERSION
        data["v2"] = {"computed_at": now - age, "margin_s": 10800, "pools": v2_pools,
                      "global_rate_tokens_per_s": None, "events": [], "curve": {}}
    return data


def store(*pools, age: float = 0.0, now: Optional[float] = None, v2: bool = True) -> None:
    pd.state_path().write_text(json.dumps(state(*pools, age=age, now=now, v2=v2)), encoding="utf-8")
