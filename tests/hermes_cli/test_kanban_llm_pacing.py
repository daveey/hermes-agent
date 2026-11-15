"""LLM quota pacing governor: pool math and Kanban dispatch admission (t_01a1d975f57bb7b8). [daveey]"""
from __future__ import annotations

import ast
import pathlib
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd

from tests.hermes_cli.test_kanban_model_tier import _write_cfg, board as _board_fixture

board = _board_fixture

OPUS = "claude-opus-5-5"
HOUR, DAY = 3600.0, 86400.0
CFG = lp.PacingConfig()


def _r(account, kind, used, reset_in, ts):
    return lp.Reading(ts, account, kind, used, ts + reset_in)


# --- pool math ---------------------------------------------------------------------------------------------

def test_pool_ahead_of_pace_is_paced():
    now = 1_800_000_000.0
    # 60% used, 10 h to reset, burning 6%/h over the last hour: 120% projected at reset.
    current = [_r("a", lp.SEVEN_DAY, 60, 10 * HOUR, now)]
    history = [_r("a", lp.SEVEN_DAY, 54, 11 * HOUR, now - HOUR)]
    pace = lp.pool_pace("anthropic", current, history, CFG)
    assert pace.state == lp.PACED
    assert pace.factor == pytest.approx((35 / (10 * HOUR)) / (6 / HOUR), rel=1e-3)


def test_pool_behind_pace_is_open():
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, 30, 3 * DAY, now), _r("a", lp.FIVE_HOUR, 10, 4 * HOUR, now)]
    history = [_r("a", lp.SEVEN_DAY, 29.5, 3 * DAY + HOUR, now - HOUR), _r("a", lp.FIVE_HOUR, 8, 5 * HOUR, now - HOUR)]
    assert lp.pool_pace("anthropic", current, history, CFG).state == lp.OPEN


def test_behind_elapsed_pace_but_projected_over_is_paced():
    """askevepodcast 11-14: 87% at 14:23:35Z, 92% at 14:29:52Z, seven_day resets 11-15 00:00Z.

    94.3% of the week had elapsed, so used-vs-elapsed called it behind pace; at +0.8%/min it hit 100% by 14:40Z."""
    reset = 1794700800.0  # 2026-11-15T00:00:00Z
    now, before = 1794666592.0, 1794666215.0  # 14:29:52Z, 14:23:35Z
    elapsed_fraction = 1 - (reset - now) / lp.WINDOW_SECONDS[lp.SEVEN_DAY]
    assert 92 / 100 < elapsed_fraction  # a used-vs-elapsed rule would not throttle
    cfg = lp.PacingConfig(reserve={lp.FIVE_HOUR: 101.0, lp.SEVEN_DAY: 101.0}, min_rate_span=300)
    current = [lp.Reading(now, "askevepodcast", lp.SEVEN_DAY, 92, reset)]
    history = [lp.Reading(before, "askevepodcast", lp.SEVEN_DAY, 87, reset)]
    pace = lp.pool_pace("anthropic", current, history, cfg)
    assert pace.state == lp.PACED and pace.factor < 0.01


def test_five_hour_bucket_gates_as_well():
    now = 1_800_000_000.0
    # Weekly is fine; the five-hour bucket went 0 -> 44% in 10 minutes (14:20-14:30Z on 11-14).
    current = [_r("a", lp.SEVEN_DAY, 20, 5 * DAY, now), _r("a", lp.FIVE_HOUR, 44, 4.83 * HOUR, now)]
    history = [_r("a", lp.FIVE_HOUR, 0, 5 * HOUR, now - 600)]
    assert lp.pool_pace("anthropic", current, history, CFG).state == lp.PACED


def test_every_account_past_reserve_holds_until_earliest_reset():
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, 100, 2 * DAY, now),
               _r("b", lp.SEVEN_DAY, 86, 9 * HOUR, now), _r("b", lp.FIVE_HOUR, 81, 3 * HOUR, now)]
    pace = lp.pool_pace("anthropic", current, [], CFG)
    assert pace.state == lp.HELD
    assert pace.until == now + 9 * HOUR  # b frees once both its reserved windows reset
    assert lp.admit_limit(pace, 5, deferrable=False, cfg=CFG) == 0


def test_one_account_under_reserve_is_not_held():
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, 100, 2 * DAY, now), _r("b", lp.SEVEN_DAY, 40, 6 * DAY, now)]
    assert lp.pool_pace("anthropic", current, [], CFG).state != lp.HELD


def test_no_readable_account_fails_open():
    pace = lp.pool_pace("anthropic", [], [], CFG)
    assert pace.state == lp.UNKNOWN
    assert lp.admit_limit(pace, 5, deferrable=False, cfg=CFG) is None


def test_paced_limit_scales_recent_concurrency_and_defers_low_tier_first():
    pace = lp.PoolPace("anthropic", lp.PACED, 0.5)
    assert lp.admit_limit(pace, 6, deferrable=False, cfg=CFG) == 3
    assert lp.admit_limit(pace, 6, deferrable=True, cfg=CFG) == 1
    assert lp.admit_limit(lp.PoolPace("anthropic", lp.OPEN, 2.0), 6, deferrable=True, cfg=CFG) is None


def test_just_behind_pace_ramps_from_recent_concurrency():
    pace = lp.PoolPace("anthropic", lp.OPEN, 1.5)
    assert lp.admit_limit(pace, 4, deferrable=False, cfg=CFG) == 6
    assert lp.admit_limit(pace, 0, deferrable=False, cfg=CFG) == 1  # off a hold: one run, then grow


def test_capped_window_burns_nothing_and_a_fresh_burst_counts_at_once():
    now = 1_800_000_000.0
    capped = _r("a", lp.SEVEN_DAY, 100, 2 * DAY, now)
    assert lp.burn_rate(capped, [], CFG) == 0.0
    current = _r("b", lp.FIVE_HOUR, 40, 4 * HOUR, now)
    history = [_r("b", lp.FIVE_HOUR, 10, 4 * HOUR + 3600, now - 3600),
               _r("b", lp.FIVE_HOUR, 20, 4 * HOUR + 600, now - 600)]
    assert lp.burn_rate(current, history, CFG) == pytest.approx(20 / 600)  # newest slope, not the hour's 30/3600


def test_config_from_reads_overrides_and_keeps_defaults():
    cfg = lp.config_from({"enabled": False, "target_percent": 90, "reserve_seven_day_percent": "x"})
    assert (cfg.enabled, cfg.target, cfg.reserve[lp.SEVEN_DAY]) == (False, 90.0, 85.0)
    assert lp.config_from(None) == lp.PacingConfig()


def test_window_kind_maps_anthropic_and_codex_labels():
    assert [lp.window_kind(x) for x in ("Current session", "Current week", "Session", "Weekly")] == [
        lp.FIVE_HOUR, lp.SEVEN_DAY, lp.FIVE_HOUR, lp.SEVEN_DAY]


# --- dispatch admission ------------------------------------------------------------------------------------

@pytest.fixture
def paced_board(board, monkeypatch):
    monkeypatch.setattr(pd, "_CACHE", {})
    return board


def _usage(monkeypatch, used, reset_in, *, kind=lp.SEVEN_DAY):
    """Every governed pool reads one account; with no history the burn is the window's average so far."""
    def read(provider, now):
        return [lp.Reading(now, f"{provider}-1", kind, used, now + reset_in)]
    monkeypatch.setattr(pd, "read_pool", read)


def _dispatch(conn):
    seen = []
    result = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, ws: seen.append(task.id))
    return seen, result


def _opus_card(conn, title="work", **kw):
    return kb.create_task(conn, title=title, assignee="default", model_override=OPUS, provider_override="anthropic", **kw)


def test_pool_ahead_of_pace_defers_spawns(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 70, 6 * DAY)  # 70% in the week's first day: ~490% projected at reset
    monkeypatch.setattr(pd, "local_running", lambda *_a: 1)  # recovery floor occupied
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, result = _dispatch(conn)
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == pd.EVENT_KIND]
        status = kb.get_task(conn, tid).status
    assert seen == [] and status == "ready"
    assert (tid, lp.GUARD_REASON) in result.respawn_guarded
    assert result.clock_held[tid][0] == lp.GUARD_REASON
    assert events and events[-1]["providers"][0]["state"] == lp.PACED


def test_pool_behind_pace_does_not_defer(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 5, 6 * DAY)  # 5% after one day: ~35% at reset
    with kbc.connect() as conn:
        a, b = _opus_card(conn, "a"), _opus_card(conn, "b")
        seen, _ = _dispatch(conn)
    assert sorted(seen) == sorted([a, b])


def test_held_pool_names_the_reset_and_repeats_no_event(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 90, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        _dispatch(conn)
        _, result = _dispatch(conn)
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == pd.EVENT_KIND]
    assert len(events) == 1 and events[0]["providers"][0]["state"] == lp.HELD
    assert abs(result.clock_held[tid][1] - (time.time() + 9 * HOUR)) < 120


@pytest.mark.parametrize("persisted", [True, False], ids=["shared-state", "memory-only"])
def test_dispatch_reuses_usage_until_read_interval_expires(paced_board, monkeypatch, persisted):
    monkeypatch.setenv("HOME", str(paced_board.parent))
    monkeypatch.setattr(pd, "_CLASS_CACHE", {})
    monkeypatch.setattr(pd, "_WARNED", {})
    interval = 60
    _write_cfg(paced_board, llm_pacing={"enabled": True, "read_interval_seconds": interval,
                                      "providers": ["anthropic"]})
    assert pd.load_config().read_interval == interval
    start = time.time()
    clock = SimpleNamespace(now=start)
    monkeypatch.setattr(pd, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))
    path = paced_board / ("shared" if persisted else "unavailable") / "llm_pacing.json"
    if persisted:
        path.parent.mkdir()
    # A failed shared-state save must not defeat the in-process read budget.
    monkeypatch.setattr(pd, "state_path", lambda: path)
    reads = []

    def read(provider, now):
        reads.append((provider, now))
        used = 90 if len(reads) == 1 else 5
        return [_r("account", lp.SEVEN_DAY, used, 6 * DAY, now)]

    monkeypatch.setattr(pd, "read_pool", read)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        for offset in (0, 1, interval - 1):
            clock.now = start + offset
            seen, result = _dispatch(conn)
            assert reads == [("anthropic", start)]
            assert seen == []
            assert (tid, lp.GUARD_REASON) in result.respawn_guarded
        assert path.exists() is persisted
        # Expiry is measured from the producer read, not the latest cache hit.
        clock.now = start + interval
        seen, _ = _dispatch(conn)
        assert reads == [("anthropic", start), ("anthropic", start + interval)]
        assert seen == [tid]


def test_governor_off_is_todays_behaviour(paced_board, monkeypatch):
    _write_cfg(paced_board, llm_pacing={"enabled": False})
    _usage(monkeypatch, 99, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, _ = _dispatch(conn)
    assert seen == [tid]


def test_mandatory_pin_is_never_throttled(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 99, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn, pin_mandatory=True)
        seen, _ = _dispatch(conn)
    assert seen == [tid]


def test_usage_read_failure_starts_the_card(paced_board, monkeypatch):
    _write_cfg(paced_board)

    def boom(provider, now):
        raise OSError("usage endpoint down")
    monkeypatch.setattr(pd, "read_pool", boom)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, _ = _dispatch(conn)
    assert seen == [tid]


def test_ungoverned_provider_is_not_throttled(paced_board, monkeypatch):
    _write_cfg(paced_board, llm_pacing={"providers": ["openai-codex"]})
    _usage(monkeypatch, 99, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, _ = _dispatch(conn)
    assert seen == [tid]


def test_only_kanban_dispatch_consults_the_governor():
    """Chats, cron and notice delivery never hold a card on it, read usage through it or write its state: only the
    dispatcher's claim path does. The one other reader is the stall notice (``kanban_parent_unclaimed``,
    t_01a1db9d9538716b), and it may take only the read-only view of the stored pace (``NOTICE_VIEW``)."""
    root = pathlib.Path(pd.__file__).resolve().parents[1]
    users = sorted(str(p.relative_to(root)) for top in ("agent", "gateway", "cron", "hermes_cli", "tui_gateway")
                   if (root / top).is_dir() for p in (root / top).rglob("*.py")
                   if "kanban_llm_pacing" in p.read_text(encoding="utf-8", errors="ignore"))
    assert users == ["hermes_cli/kanban_db_dispatch.py", "hermes_cli/kanban_db_edit.py",
                     "hermes_cli/kanban_dispatch_maintenance.py",
                     "hermes_cli/kanban_llm_pacing.py", "hermes_cli/kanban_llm_pacing_dispatch.py",
                     "hermes_cli/kanban_paced_reroute.py",
                     "hermes_cli/kanban_parent_unclaimed.py", "hermes_cli/kanban_worker_node.py"]
    assert _governor_names(root / "hermes_cli/kanban_dispatch_maintenance.py") == {
        "kanban_llm_pacing_dispatch": {"refresh_pools"}}
    assert _governor_names(root / "hermes_cli/kanban_parent_unclaimed.py") == NOTICE_VIEW
    # ``hermes kanban edit`` refuses an unreasoned raise on a held card (agent-6f3c6009b5): the same read-only view.
    assert _governor_names(root / "hermes_cli/kanban_db_edit.py") == EDIT_VIEW
    assert _governor_names(root / "hermes_cli/kanban_worker_node.py") == PICKUP_VIEW
    assert _governor_names(root / "hermes_cli/kanban_paced_reroute.py") == REROUTE_VIEW


# What the stall notice may take from the governor: no hold(), no usage read, no state write.
NOTICE_VIEW = {"kanban_llm_pacing_dispatch": {"admission", "admission_skip", "paced_pools"}}
EDIT_VIEW = {"kanban_llm_pacing_dispatch": {"paced_pools"}}
# Worker pickup (t_01a1ddeb76e30be7) predicts the claim-time hold on the replica's copy, so a card dispatch would
# hold takes no pickup budget: its one entry point holds nothing, records no card event and fails open.
PICKUP_VIEW = {"kanban_llm_pacing_dispatch": {"pickup_hold"}}
# The route resolver moves a route off a far held pool (t_01a1e026c2ba361b): the stored holds only, no usage read.
REROUTE_VIEW = {"kanban_llm_pacing_dispatch": {"held_pools"}}


def _governor_names(path: pathlib.Path) -> dict[str, set[str]]:
    """Each governor module ``path`` imports, with every name it imports from it or reads off it as ``alias.name``.
    A dotted ``import`` of one, or its alias used any other way (passed on, stored), is listed as such."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    aliases: dict[str, str] = {}
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "kanban_llm_pacing" in alias.name:
                    names.setdefault(alias.name, set()).add("<module import>")
        elif isinstance(node, ast.ImportFrom) and "kanban_llm_pacing" in (node.module or ""):
            names.setdefault(node.module.rsplit(".", 1)[-1], set()).update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if "kanban_llm_pacing" in alias.name:
                    aliases[alias.asname or alias.name] = alias.name
                    names.setdefault(alias.name, set())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in aliases and isinstance(node.ctx, ast.Load):
            parent = parents.get(node)
            names[aliases[node.id]].add(parent.attr if isinstance(parent, ast.Attribute) and parent.value is node
                                        else "<bare module use>")
    return names


@pytest.mark.parametrize("source, seen", [
    ("from hermes_cli import kanban_llm_pacing_dispatch as p\np.admission(p.paced_pools(c, t))\np.admission_skip(a)\n",
     NOTICE_VIEW),
    ("from hermes_cli import kanban_llm_pacing_dispatch as p\np.paced_pools(c, t)\np.hold(c, r, 'ready', x)\n",
     {"kanban_llm_pacing_dispatch": {"paced_pools", "hold"}}),
    ("from hermes_cli import kanban_llm_pacing_dispatch as p\nkeep(p)\n", {"kanban_llm_pacing_dispatch": {"<bare module use>"}}),
    ("from hermes_cli.kanban_llm_pacing_dispatch import read_pool\n", {"kanban_llm_pacing_dispatch": {"read_pool"}}),
    ("import hermes_cli.kanban_llm_pacing_dispatch\n", {"hermes_cli.kanban_llm_pacing_dispatch": {"<module import>"}}),
    ("from hermes_cli import kanban_llm_pacing as lp\nlp.admit_limit(x)\n", {"kanban_llm_pacing": {"admit_limit"}}),
])
def test_the_notice_view_pin_sees_every_way_to_reach_the_governor(tmp_path, source, seen):
    """The pin above holds only if ``_governor_names`` reports a hold(), a usage read, the module passed on or a
    dotted import; anything beyond the allowed view must differ from ``NOTICE_VIEW``."""
    module = tmp_path / "notice.py"
    module.write_text(source, encoding="utf-8")
    assert _governor_names(module) == seen
