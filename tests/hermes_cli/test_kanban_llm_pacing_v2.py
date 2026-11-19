"""``llm_pacing.json`` v2 IO: the start hold reads ``v2.pools[p].at_reserve``, an old file upgrades in place, the
history stores hashes only and the token source is per provider. [daveey]

Labels here are synthetic (``*.test``); none may appear in ``v2`` or the history file."""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_llm_pacing_history as hist
from tests.hermes_cli import kanban_pacing_state as pacing_state
from tests.hermes_cli import test_kanban_stuck_clock_holds as helpers

home = helpers.home
LABEL = "pacing-account-one@example.test"
OTHER = "pacing-account-two@example.test"
DAY = 86400.0
REAL_FLEET_TOKENS = hist.fleet_tokens


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(pd, "_CACHE", {})
    monkeypatch.setattr(pd, "_WARNED", {})
    monkeypatch.setattr(pd, "_MODEL_SCOPED", {})
    monkeypatch.setattr(pd, "_V2_MEMO", {})
    monkeypatch.setattr(hist, "fleet_tokens", lambda provider: None)


def _cfg(**kw):
    return lp.PacingConfig(providers=("anthropic",), **kw)


def _governed(monkeypatch, cfg):
    """The card is governed on anthropic and has no alternate; ``pace_for`` makes no read (the stored file rules)."""
    monkeypatch.setattr(pd, "load_config", lambda: cfg)
    monkeypatch.setattr(pd, "_governed", lambda conn, task_id, lane, cfg: (SimpleNamespace(id=task_id), ["anthropic"]))
    monkeypatch.setattr(pd, "pace_for", lambda *a: None)
    monkeypatch.setattr(pd, "_pace_alternate", lambda *a, **k: None)


def _result():
    return SimpleNamespace(respawn_guarded=[], clock_held={}, llm_paced=[])


def test_the_hold_readers_follow_v2_at_reserve_and_fail_open_without_it(home, monkeypatch):
    cfg = _cfg()
    _governed(monkeypatch, cfg)
    until = time.time() + 7200
    pacing_state.store(("anthropic", lp.HELD, until))
    result = _result()
    assert pd.hold(None, {"id": "t_1"}, "ready", result, dry_run=True) is True
    assert result.llm_paced[0][3] == {"providers": [{"provider": "anthropic", "state": lp.HELD, "limit": 0}]}
    assert pd.held_pools({"providers": ["anthropic"]}, time.time()) == {"anthropic": until}
    assert [(p.provider, p.state, p.until) for p in pd.paced_pools(None, "t_1")] == [("anthropic", lp.HELD, until)]
    assert pd.pickup_hold(None, None, {"id": "t_1", "status": "ready"}) == "anthropic held"

    # The same legacy ``held`` pace with no v2 (an old file) holds nothing: fail open.
    pacing_state.store(("anthropic", lp.HELD, until), v2=False)
    assert pd.hold(None, {"id": "t_1"}, "ready", _result(), dry_run=True) is False
    assert pd.held_pools({"providers": ["anthropic"]}, time.time()) == {}
    assert pd.paced_pools(None, "t_1") is None
    assert pd.pickup_hold(None, None, {"id": "t_1", "status": "ready"}) is None

    # v2 under reserve holds nothing although the legacy verdict says held; a stale v2 holds nothing either.
    data = pacing_state.state(("anthropic", lp.HELD, until))
    data["v2"]["pools"]["anthropic"]["at_reserve"] = False
    pd.state_path().write_text(json.dumps(data), encoding="utf-8")
    assert pd.hold(None, {"id": "t_1"}, "ready", _result(), dry_run=True) is False
    pacing_state.store(("anthropic", lp.HELD, until), age=pd.STALE_READ_INTERVALS * cfg.read_interval + 1)
    assert pd.held_pools({"providers": ["anthropic"]}, time.time()) == {}


def test_a_hold_this_process_planned_survives_a_failed_shared_save(home, monkeypatch):
    """The legacy in-process pace cache kept a held verdict when the shared file could not be written; the v2 hold
    keeps it too (``_V2_MEMO``), while another process reading the file sees no hold (fail open)."""
    cfg = _cfg()
    monkeypatch.setattr(pd, "load_config", lambda: cfg)
    monkeypatch.setattr(pd, "_governed", lambda conn, task_id, lane, cfg: (SimpleNamespace(id=task_id), ["anthropic"]))
    monkeypatch.setattr(pd, "_pace_alternate", lambda *a, **k: None)
    monkeypatch.setattr(pd, "_save_state", lambda *a: None)
    _reads(monkeypatch, lambda t: [(LABEL, lp.SEVEN_DAY, 90.0, t + DAY)])
    assert pd.hold(None, {"id": "t_1"}, "ready", _result(), dry_run=True) is True
    assert not pd.state_path().exists() and pd.held_pools({"providers": ["anthropic"]}, time.time()) == {}


def test_a_paced_pool_holds_no_start(home, monkeypatch):
    _governed(monkeypatch, _cfg())
    pacing_state.store(("anthropic", lp.PACED))
    assert pd.hold(None, {"id": "t_1"}, "ready", _result(), dry_run=True) is False
    assert pd.paced_pools(None, "t_1") is None


def _reads(monkeypatch, windows):
    def read(provider, now):
        return [lp.Reading(now, label, kind, used, reset) for label, kind, used, reset in windows(now)]
    monkeypatch.setattr(pd, "read_pool", read)


def test_an_old_file_is_upgraded_on_the_next_refresh_and_keeps_its_legacy_keys(home, monkeypatch):
    now = time.time()
    legacy = {
        "anthropic": {"read_at": now - 10, "pace": lp.PoolPace("anthropic", lp.OPEN).as_dict(),
                      "running": [[now - 10, 1]], "samples": [[now - 10, LABEL, lp.SEVEN_DAY, 40.0, now + DAY]]},
        "openai-codex": {"read_at": now - 10, "pace": lp.PoolPace("openai-codex", lp.PACED, 0.5).as_dict(),
                         "samples": [[now - 10, OTHER, lp.SEVEN_DAY, 70.0, now + 2 * DAY]]},
    }
    pd._save_state(pd.state_path(), legacy)
    _reads(monkeypatch, lambda t: [(LABEL, lp.SEVEN_DAY, 41.0, now + DAY)])
    cfg = _cfg()
    pd.pace_for(None, "anthropic", cfg, now)  # read_at is fresh, yet no v2: it reads and upgrades
    data = json.loads(pd.state_path().read_text())
    assert data["version"] == lp.V2_VERSION
    assert data["openai-codex"] == legacy["openai-codex"]  # another provider's legacy key is untouched
    assert set(data["anthropic"]) == {"read_at", "pace", "samples"}
    assert data["anthropic"]["samples"][-1][1] == LABEL  # legacy samples keep the label (runway reads them)
    pool = data["v2"]["pools"]["anthropic"]
    assert pool["at_reserve"] is False and pool["read_at"] == now and data["v2"]["computed_at"] == now
    assert pool["accounts"] == [{"id": lp.account_id(LABEL), "kind": lp.SEVEN_DAY, "used": 41.0,
                                 "reset_at": now + DAY, "tpp": None}]
    assert LABEL not in json.dumps(data["v2"])


def test_the_history_keeps_hashes_and_model_scoped_windows_but_never_counts_them(home, monkeypatch):
    now = time.time()

    def read(provider, ts):
        pd._MODEL_SCOPED[provider] = [lp.Reading(ts, LABEL, "seven_day_opus", 99.0, now + DAY)]
        return [lp.Reading(ts, LABEL, lp.SEVEN_DAY, 30.0, now + 3 * DAY)]
    monkeypatch.setattr(pd, "read_pool", read)
    monkeypatch.setattr(hist, "fleet_tokens", lambda provider: {"hub": 1000.0, "worker-1": 50.0})
    cfg = _cfg(tokens_per_percent={"anthropic": {lp.SEVEN_DAY: 1000.0}})
    pd.pace_for(None, "anthropic", cfg, now)
    path = pd.history_path()
    assert LABEL.encode() not in path.read_bytes()
    with sqlite3.connect(path) as db:
        kinds = sorted(k for (k,) in db.execute("SELECT kind FROM samples"))
        accounts = {a for (a,) in db.execute("SELECT account_hash FROM samples")}
        tokens = db.execute("SELECT provider, cumulative_io FROM tokens").fetchall()
    assert kinds == [lp.SEVEN_DAY, "seven_day_opus"] and accounts == {lp.account_id(LABEL)}
    assert tokens == [("anthropic", 0.0)]  # the first point: every machine is new, nothing counted as spend
    pool = json.loads(pd.state_path().read_text())["v2"]["pools"]["anthropic"]
    assert [a["kind"] for a in pool["accounts"]] == [lp.SEVEN_DAY]  # Opus at 99% changes nothing
    assert pool["at_reserve"] is False and pool["tpp_source"] == lp.SEED
    assert pool["remaining_tokens"] == (85.0 - 30.0) * 1000.0


def test_fleet_tokens_advance_by_per_machine_increases_only(tmp_path):
    path = tmp_path / hist.FILE_NAME
    now = 1_800_000_000.0
    for i, machines in enumerate(({"hub": 100.0}, {"hub": 150.0, "new": 10_000.0}, {"hub": 120.0, "new": 10_050.0},
                                  {"hub": 170.0, "new": 10_050.0})):
        with hist.History.open(path, "anthropic") as store:
            store.record(now + i * 300, [], machines, [], 8 * DAY)
    with hist.History.open(path, "anthropic") as store:
        assert [v for _, v in store.tokens(0)] == [0.0, 50.0, 100.0, 150.0]  # joins and a drop are no spend
        store.record(now + 9 * DAY, [], {"hub": 200.0}, [], 8 * DAY)
        assert [v for _, v in store.tokens(0)] == [180.0]  # older rows pruned after history_days
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == hist.SCHEMA_VERSION


def test_registry_machines_report_provider_tokens_else_model_prefixes(monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli import machine_throughput_metrics as mt
    monkeypatch.setattr(wn, "registry", lambda **kw: {"me": "hub", "machines": {
        "hub": {"metrics": {"tokens_by_model": {"claude-opus-9": [5, 0]}}},
        "worker-1": {"metrics": {"tokens_by_provider": {"anthropic": [70, 3], "openai-codex": [9, 0]}}},
        "worker-2": {"metrics": {"tokens_by_model": {"claude-sonnet-9": [11, 1], "gpt-6": [400, 0]}}},
        "worker-3": {"metrics": {}}}})
    monkeypatch.setattr(mt, "provider_counters", lambda: {"tokens_by_provider": {"anthropic": [42, 0]}})
    assert REAL_FLEET_TOKENS("anthropic") == {"hub": 42.0, "worker-1": 70.0, "worker-2": 11.0}


def test_provider_counters_group_tokens_by_billing_provider(tmp_path):
    from hermes_cli import machine_throughput_metrics as mt
    mt.reset_cache()
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, ended_at REAL)")
    db.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, "
               "input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT)")
    db.executemany("INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("s1", "claude-opus-9", "anthropic", 10, 5, 100, 0),
        ("s2", "claude-opus-9", "openrouter", 7, 0, 0, 0),
        ("s3", "gpt-6", "", 20, 2, 0, 1),
        ("s4", "grok-5", None, 3, 0, 0, 0)])
    db.commit()
    db.close()
    assert mt.provider_counters(tmp_path)["tokens_by_provider"] == {
        "anthropic": [15, 100], "openrouter": [7, 0], "openai-codex": [22, 1], "xai-oauth": [3, 0]}
    counters = mt.counters(tmp_path)
    assert counters["tokens_io_total"] == 47 and "tokens_by_provider" not in counters  # the 10 s path is unchanged
