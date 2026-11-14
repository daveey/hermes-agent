"""LLM pacing governor follow-ups (t_01a1dafcb027ff7b; review of PR #3025). [daveey]

1. A pacing hold wrote ``llm_paced`` but no recognised skip, so queue position, the chat-wait and
   parent-unclaimed notices read a paced card as "dispatcher skip reason unrecorded" after 30 min, and
   ``kanban diagnostics`` called it stranded. It now records ``dispatch_skipped {reason: llm_paced}``.
2. ``/api/oauth/usage`` reports an idle account's ``five_hour`` as ``null`` (6 of 7 accounts, 11-14 16:57Z), so the
   account had no five_hour reading and added 0 sustainable rate: one other account's 5h burn drove the factor
   to 0 (PACED, limit 0) while the idle account was free. A missing window (or one with no reset) now counts as a
   fresh full window.
3. Dispatch-level coverage for class selectors, a non-zero paced limit and the deferrable share.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as qp
from hermes_cli.kanban_diagnostics import compute_task_diagnostics
from tests.hermes_cli.test_kanban_model_tier import board as _board_fixture

board = _board_fixture

OPUS = "claude-opus-5-5"
HOUR, DAY = 3600.0, 86400.0
CFG = lp.PacingConfig()
PRIORITY_WORDS = ("bump", "priority to")


# --- 2. idle account: five_hour null ------------------------------------------------------------------------

def test_idle_account_without_a_five_hour_reading_adds_a_full_window_of_headroom():
    """The 11-14 16:57Z pool, reduced: askevepodcast burns its 5h window; softmax.admin has 7d headroom and an
    idle (``null``) five_hour. Before the fix the 5h sustainable rate was 0 -> factor 0 -> PACED, limit 0."""
    now = 1794675425.0
    current = [lp.Reading(now, "askevepodcast", lp.FIVE_HOUR, 78, 1794687600.0),  # 19:20Z
               lp.Reading(now, "askevepodcast", lp.SEVEN_DAY, 100, 1794700800.0),
               lp.Reading(now, "softmax.admin", lp.SEVEN_DAY, 34, 1795276800.0)]  # 11-21 16:00Z
    history = [lp.Reading(now - HOUR, "askevepodcast", lp.FIVE_HOUR, 70, 1794687600.0),
               lp.Reading(now - HOUR, "softmax.admin", lp.SEVEN_DAY, 34, 1795276800.0)]  # 7d flat: 5h decides
    pace = lp.pool_pace("anthropic", current, history, CFG)
    five_hour = (95 / lp.WINDOW_SECONDS[lp.FIVE_HOUR]) / (8 / HOUR)  # softmax.admin's fresh window vs 8%/h
    assert pace.factor == pytest.approx(five_hour, abs=1e-4)
    assert pace.state == lp.OPEN  # was PACED at factor 0, limit 0
    assert lp.admit_limit(pace, 4, deferrable=False, cfg=CFG) is None


def test_a_window_with_no_reset_counts_as_a_full_window():
    now = 1_800_000_000.0
    current = [lp.Reading(now, "a", lp.FIVE_HOUR, 60, now + HOUR),
               lp.Reading(now, "b", lp.FIVE_HOUR, 0, None)]
    history = [lp.Reading(now - HOUR, "a", lp.FIVE_HOUR, 30, now + HOUR)]
    pace = lp.pool_pace("anthropic", current, history, CFG)
    expected = ((95 - 60) / HOUR + 95 / lp.WINDOW_SECONDS[lp.FIVE_HOUR]) / (30 / HOUR)
    assert pace.factor == pytest.approx(expected, rel=1e-3)


def test_a_reserved_account_adds_no_headroom_even_without_a_reading():
    """Only headroom accounts count: an account past its 7d reserve adds nothing for its missing five_hour."""
    now = 1_800_000_000.0
    current = [lp.Reading(now, "a", lp.FIVE_HOUR, 60, now + HOUR),
               lp.Reading(now, "b", lp.SEVEN_DAY, 100, now + DAY)]  # capped: reserved, burns nothing more
    history = [lp.Reading(now - HOUR, "a", lp.FIVE_HOUR, 30, now + HOUR)]
    pace = lp.pool_pace("anthropic", current, history, CFG)
    assert pace.factor == pytest.approx(((95 - 60) / HOUR) / (30 / HOUR), rel=1e-3)


# --- config registration ------------------------------------------------------------------------------------

def test_registered_config_defaults_match_the_governor_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.kanban_pacing_defaults import DEFAULTS
    assert DEFAULT_CONFIG["kanban"]["llm_pacing"] == DEFAULTS
    assert lp.config_from(DEFAULT_CONFIG["kanban"]["llm_pacing"]) == lp.PacingConfig()
    assert set(DEFAULTS) == {"enabled", "target_percent", "reserve_five_hour_percent", "reserve_seven_day_percent",
                             "providers", "read_interval_seconds", "rate_window_seconds", "min_rate_span_seconds",
                             "deferrable_share"}


# --- dispatch fixtures --------------------------------------------------------------------------------------

@pytest.fixture
def paced_board(board, monkeypatch):
    monkeypatch.setattr(pd, "_CACHE", {})
    monkeypatch.setattr(pd, "_CLASS_CACHE", {})
    return board


def _config(home, **extra):
    # JSON is valid YAML (the test venv may have no PyYAML).
    home.joinpath("config.yaml").write_text(json.dumps({"kanban": {}, **extra}), encoding="utf-8")


def _paced_reads(monkeypatch):
    """Every pool reads 20% used one day into the week: factor 0.625 (paced) with no history."""
    def read(provider, now):
        return [lp.Reading(now, f"{provider}-1", lp.SEVEN_DAY, 20, now + 6 * DAY)]
    monkeypatch.setattr(pd, "read_pool", read)


def _seed_concurrency(running: float):
    """This host ran ``running`` cards on anthropic over the last rate window (the paced limit's base)."""
    now = time.time()
    pd.state_path().write_text(json.dumps({"anthropic": {"running": [[now - 60 * i, running] for i in range(1, 30)]}}),
                               encoding="utf-8")


def _tick(conn):
    seen = []
    result = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, ws: seen.append(task.id))
    return seen, result


def _opus(conn, title, **kw):
    return kb.create_task(conn, title=title, assignee="default", model_override=OPUS, provider_override="anthropic",
                          **kw)


def _skips(conn, tid):
    return [kb._json_dict(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, skips.EVENT))]


# --- 1. paced holds are recorded skips ----------------------------------------------------------------------

def test_paced_hold_records_a_skip_the_notices_and_diagnostics_name(paced_board, monkeypatch):
    _config(paced_board)
    _paced_reads(monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *_a: (1, 7))  # free slots: the unrecorded case
    start = time.time()
    with kbc.connect() as conn:
        tid = _opus(conn, "paced", session_id="chat")
        seen, result = _tick(conn)
        assert seen == [] and kb.get_task(conn, tid).status == "ready"
        assert result.llm_paced and result.llm_paced[0][:3] == (tid, "anthropic paced", None)
        [skip] = _skips(conn, tid)
        assert (skip["reason"], skip["detail"]) == (skips.LLM_PACED, "anthropic paced")
        assert skip["providers"] == [{"provider": "anthropic", "state": lp.PACED, "limit": 0, "running": 0}]
        _tick(conn)
        assert len(_skips(conn, tid)) == 1  # one row per episode, not one per tick

        task = kb.get_task(conn, tid)
        place = qp.position(conn, task)
        assert place["skip"]["reason"] == skips.LLM_PACED
        line = qp.describe(place)
        assert "held: LLM quota pacing (anthropic paced pool)" in line
        assert not any(w in line for w in PRIORITY_WORDS), line

        later = start + 31 * 60
        assert not skips.notice_fields(place, 31 * 60).get("skip_unrecorded")
        monkeypatch.setattr(kb.time, "time", lambda: later)
        assert cw.sweep(conn, now=later) == 1
        payload = kb._json_dict(conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                             (tid, cw.NOTICE_KIND)).fetchone()[0])
        assert payload["dispatch_skip"] == {"reason": skips.LLM_PACED, "detail": "anthropic paced"}
        assert "skip_unrecorded" not in payload
        for text in (cw.notice_text(payload), pu.notice_text(payload), pu.ready_hint(conn, task, now=later)):
            assert "held: LLM quota pacing (anthropic paced pool)" in text, text
            assert skips.UNRECORDED not in text
            assert not any(w in text for w in PRIORITY_WORDS), text

        diags = compute_task_diagnostics(task, kb.list_events(conn, tid), kb.list_runs(conn, tid), now=later)
    assert [(d.kind, d.severity) for d in diags] == [("held_in_ready", "warning")]
    assert diags[0].data["hold_reason"] == "llm_paced"
    assert "llm_paced: anthropic paced" in diags[0].title


def test_held_pool_skip_carries_the_reset(paced_board, monkeypatch):
    _config(paced_board)
    reset = [0.0]

    def read(provider, now):
        reset[0] = now + 9 * HOUR
        return [lp.Reading(now, f"{provider}-1", lp.SEVEN_DAY, 90, reset[0])]
    monkeypatch.setattr(pd, "read_pool", read)
    with kbc.connect() as conn:
        tid = _opus(conn, "held")
        _tick(conn)
        [skip] = _skips(conn, tid)
        assert (skip["reason"], skip["detail"], skip["until"]) == (skips.LLM_PACED, "anthropic held", int(reset[0]))
        diags = compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid), kb.list_runs(conn, tid),
                                         now=time.time() + 3600)
        assert diags[0].data["hold_until"] == int(reset[0])
        # Past the reset the old skip no longer reads as a live pacing hold.
        late = compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid), kb.list_runs(conn, tid),
                                        now=reset[0] + 60)
    assert all(d.data.get("hold_reason") != "llm_paced" for d in late)


def test_dry_run_records_the_hold_on_the_result_only(paced_board, monkeypatch):
    _config(paced_board)
    _paced_reads(monkeypatch)
    with kbc.connect() as conn:
        tid = _opus(conn, "paced")
        result = kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *_a: None)
        assert [row[0] for row in result.llm_paced] == [tid]
        assert _skips(conn, tid) == []


# --- 3. dispatch-level limits -------------------------------------------------------------------------------

def test_paced_limit_is_a_share_of_recent_concurrency_counted_against_running_cards(paced_board, monkeypatch):
    """avg 4 running x factor 0.625 -> limit 2: two cards start, the third waits for a running one to end."""
    _config(paced_board)
    _paced_reads(monkeypatch)
    _seed_concurrency(4)
    with kbc.connect() as conn:
        ids = [_opus(conn, f"c{i}") for i in range(3)]
        seen, result = _tick(conn)
        held = [row[0] for row in result.llm_paced]
    assert len(seen) == 2 and held == [t for t in ids if t not in seen]
    assert result.llm_paced[0][3]["providers"][0] == {"provider": "anthropic", "state": lp.PACED, "limit": 2,
                                                       "running": 2}


def test_deferrable_card_gets_only_its_share_of_the_paced_limit(paced_board, monkeypatch):
    """Limit 2, deferrable share 0.5 -> 1: the normal card starts, the negative-priority card then waits."""
    _config(paced_board)
    _paced_reads(monkeypatch)
    _seed_concurrency(4)
    with kbc.connect() as conn:
        normal = _opus(conn, "normal")
        low = _opus(conn, "low", priority=-1)
        seen, result = _tick(conn)
    assert seen == [normal]
    [(tid, _detail, _until, extra)] = result.llm_paced
    assert tid == low
    assert extra["providers"][0]["limit"] == 1 and extra["providers"][0]["running"] == 1


ROUTE = {"billing": "subscription", "weight": 1, "eligible": True, "funding_verified": True,
         "capabilities": {"tools": True, "streaming": True, "context_tokens": 1000000}}


def _class_config(home, *providers):
    routes = [dict(ROUTE, provider=p, model=f"{p}-deep") for p in providers]
    _config(home, model_routing={"enabled": True, "routes": {"deep": routes}})


def test_class_selector_is_paced_on_its_class_providers(paced_board, monkeypatch):
    _class_config(paced_board, "anthropic")
    _paced_reads(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deep", assignee="default", model_override="class:deep",
                             provider_override="auto")
        seen, result = _tick(conn)
    assert seen == [] and [row[0] for row in result.llm_paced] == [tid]


def test_class_selector_with_an_ungoverned_route_always_starts(paced_board, monkeypatch):
    """Any ungoverned candidate admits the card: it may run there whatever the governed pools say."""
    _class_config(paced_board, "anthropic", "zai")
    _paced_reads(monkeypatch)  # every pool, zai included, reads paced
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deep", assignee="default", model_override="class:deep",
                             provider_override="auto")
        seen, result = _tick(conn)
    assert seen == [tid] and result.llm_paced == []
