"""Governor snapshot and ``why``: what a real dispatcher tick decided, read back. [daveey]

Every test here runs the real dispatcher (``dispatch_once``) on real board DBs under a temp ``HERMES_HOME`` and then
reads ``kanban_governors.snapshot`` / ``hermes kanban why``. Only what leaves the machine is replaced: the worker
spawn (a pid) and, for pacing, the provider usage read (``read_pool``), which here returns the account windows a
live read would. The pacing verdict, the lane gate, the host-slot census and the placement refusal are the
dispatcher's own.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_governors as governors
from hermes_cli import kanban_governors_cli as gcli
from hermes_cli import kanban_governors_explain as explain
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from tests.hermes_cli import test_kanban_label_backlog_wait as pool_wait
from tests.hermes_cli import test_kanban_model_tier as model_tier

board = model_tier.board  # one unsynced home whose class routes all land on anthropic
pool_hub = pool_wait.hub  # a synced hub that cannot lease ``pool:worker`` cards

DAY = 86400
RESERVE = lp.PacingConfig().reserve[lp.SEVEN_DAY]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_TENANT", "HERMES_KANBAN_BOARD",
                 "HERMES_KANBAN_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "normal")  # this machine's RAM is not the test
    for cache in (pd._CACHE, pd._V2_MEMO, pd._CLASS_CACHE):
        monkeypatch.setattr(pd, {id(pd._CACHE): "_CACHE", id(pd._V2_MEMO): "_V2_MEMO",
                                 id(pd._CLASS_CACHE): "_CLASS_CACHE"}[id(cache)], {})
    monkeypatch.setattr(lanes, "_LAST_GOOD", [])
    monkeypatch.setattr(lanes, "_WARNED", set())


def _usage(monkeypatch, used):
    """The provider usage read answers ``used[0]`` percent on one account's 7-day window (mutable: recovery)."""
    def read_pool(provider, now):
        return [lp.Reading(now, "acct-1@pool.test", lp.SEVEN_DAY, float(used[0]), now + 3 * DAY)] \
            if provider == "anthropic" else []
    monkeypatch.setattr(pd, "read_pool", read_pool)


def _tick(conn, spawned=None, **kw):
    def spawn(task, workspace, board=None):
        if spawned is not None:
            spawned.append(task.id)
        return os.getpid()
    return kbd.dispatch_once(conn, spawn_fn=spawn, **kw)


def _row(snap, governor, scope=None):
    return next(r for r in snap["governors"] if r["governor"] == governor and r.get("scope") == scope)


def _sums(snap):
    assert snap["available"] and not snap["stale"]
    assert sum(r["cards"] for r in snap["governors"]) == snap["idle_total"] == snap["charged_total"]


def _why(tid, *flags):
    return json.loads(kcli.run_slash(" ".join(["why", tid, "--json", *flags])))


def test_registry_names_every_governor_once_with_a_live_source():
    import importlib

    rows = governors.registry()
    assert len({r["id"] for r in rows}) == len(rows) >= 35
    for row in rows:
        assert set(row) == {"id", "label", "scope", "source", "threshold", "kind"} and all(row.values()), row
        assert row["kind"] in ("hold", "throttle", "reorder")
        path, name = row["source"].split(":")
        module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
        assert callable(getattr(module, name)), row["source"]  # the predicate the dispatcher calls exists
    # every card-level governor ``why`` walks is registered, in the registry's (dispatcher) order
    walked = [name for name, _ in explain.CARD_STEPS]
    assert walked == sorted(walked, key=governors.order_of) and set(walked) <= {r["id"] for r in rows}


def test_pacing_pool_at_reserve_holds_the_card_then_recovers(board, monkeypatch):
    """Acceptance 1 and 2."""
    used = [RESERVE + 7]
    _usage(monkeypatch, used)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs anthropic", assignee="default")
        spawned = []
        result = _tick(conn, spawned)
        assert spawned == [] and [p[0] for p in result.llm_paced] == [tid]

        snap = governors.snapshot(conn)
        _sums(snap)
        pool = _row(snap, "llm_pool")
        assert (pool["active"], pool["cards"], pool["card_ids"]) == (True, 1, [tid])
        assert pool["reading"] == {"used_pct": RESERVE + 7, "accounts_under_reserve": 0, "accounts": 1}
        assert pool["threshold"][lp.SEVEN_DAY] == RESERVE
        assert [r["governor"] for r in snap["governors"]].count("llm_pool") == 1  # one pool governor, not one a provider
        anthropic = pool["detail"]["providers"]["anthropic"]  # per-provider numbers are detail on that one entry
        assert (anthropic["used_pct"], anthropic["accounts_under_reserve"], anthropic["at_reserve"]) == (RESERVE + 7, 0, True)
        assert set(pool["detail"]["providers"]) == set(lp.PacingConfig().providers)
        assert pool["since"] is not None
        assert snap["cards"][tid]["governor"] == "llm_pool" and snap["cards"][tid]["overlaps"] == []

        for flags in ((), ("--live",)):  # from the snapshot, and the read-only dry run: the same first stop
            answer = _why(tid, *flags)
            first = answer["first_stop"]
            assert answer["source"] == ("dry_run" if flags else "snapshot")
            assert (first["governor"], first["scope"]) == ("llm_pool", None), answer
            assert (first["reading"]["used_pct"], first["reading"]["accounts_under_reserve"]) == (RESERVE + 7, 0)
            assert first["threshold"][lp.SEVEN_DAY] == RESERVE
            assert first["detail"]["card_routes_at_reserve"] == ["anthropic"]
            assert first["source"] == "hermes_cli/kanban_llm_pacing_dispatch.py:hold"
        chain = _why(tid, "--live")["chain"]
        assert [s["governor"] for s in chain if s["verdict"] == "stop"] == ["llm_pool"]
        assert [s["governor"] for s in chain] == sorted((s["governor"] for s in chain), key=governors.order_of)
        assert "stopped by LLM pool" in kcli.run_slash(f"why {tid}")
        assert kb.get_task(conn, tid).status == "ready"  # ``why`` moved nothing

        # Recovery: the pool reads under its reserve, the next tick claims the card, the governor is inactive.
        used[0] = RESERVE - 45
        monkeypatch.setattr(pd, "_CACHE", {})
        state = json.loads(pd.state_path().read_text())
        state["anthropic"]["read_at"] -= lp.PacingConfig().read_interval + 1  # the read interval has passed
        pd.state_path().write_text(json.dumps(state))
        _tick(conn, spawned)
        assert spawned == [tid] and kb.get_task(conn, tid).status == "running"
        snap = governors.snapshot(conn)
        _sums(snap)
        pool = _row(snap, "llm_pool")
        assert (pool["active"], pool["cards"], pool["since"]) == (False, 0, None)
        assert pool["reading"] == {"used_pct": RESERVE - 45, "accounts_under_reserve": 1, "accounts": 1}
        assert snap["idle_total"] == 0 and tid not in snap["cards"]
        assert _why(tid)["first_stop"] is None


def test_gated_lane_at_admit_per_day_charges_held_cards_to_the_lane_gate(board, monkeypatch):
    """Acceptance 3."""
    monkeypatch.setattr(lanes, "_human_shell", lambda: True)
    model_tier._write_cfg(board, admission={"agent_priority_cap": 100, "lanes": {
        "hermes": {"share": 20, "gated": True, "admit_per_day": 2},
        "other": {"share": 10}}})
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="core work", assignee="default", created_by="user",
                                tenant="hermes")
        held = [kb.create_task(conn, title=f"follow-up {i}", assignee="default", creator_task_id=parent,
                               created_by="default", tenant="hermes") for i in range(4)]
        assert {kb.get_task(conn, t).status for t in held} == {"triage"}
        for tid in held[:2]:  # the day's budget: two admitted
            assert lanes.admit(conn, tid, actor="user", reason="today's budget")
        assert lanes.admitted_today(conn, "hermes") == 2
        spawned = []
        _tick(conn, spawned, max_in_progress=1)

        snap = governors.snapshot(conn)
        _sums(snap)
        gate = _row(snap, "lane_gate", "hermes")
        assert (gate["active"], gate["cards"], sorted(gate["card_ids"])) == (True, 2, sorted(held[2:]))
        assert (gate["reading"], gate["threshold"]) == (2, 2)  # admitted today / admit_per_day
        for tid in held[2:]:
            assert (snap["cards"][tid]["governor"], snap["cards"][tid]["scope"]) == ("lane_gate", "hermes")
            first = _why(tid)["first_stop"]
            assert (first["governor"], first["reading"], first["threshold"]) == ("lane_gate", 2, 2)
            assert _why(tid, "--live")["first_stop"]["governor"] == "lane_gate"
        # the cards the one host slot could not start are charged to the host, not the gate
        waiting = [t for t in (parent, *held[:2]) if t not in spawned]
        assert len(spawned) == 1 and {snap["cards"][t]["governor"] for t in waiting} == {"host_slots"}


def test_host_slots_are_charged_per_board_and_release_when_the_other_board_finishes(board):
    """Acceptance 4."""
    kb.create_board("beta")
    with kbc.connect(board="default") as a, kbc.connect(board="beta") as b:
        a_ids = [kb.create_task(a, title=f"a{i}", assignee="default") for i in range(2)]
        b_ids = [kb.create_task(b, title=f"b{i}", assignee="default") for i in range(2)]
        started = []
        _tick(a, started, max_in_progress=2, board="default")
        assert sorted(started) == sorted(a_ids)
        result = _tick(b, started, max_in_progress=2, board="beta")
        assert sorted(started) == sorted(a_ids) and result.at_capacity == "max_in_progress=2/2"

        snap = governors.snapshot(b)
        _sums(snap)
        host = _row(snap, "host_slots")
        assert (host["active"], host["cards"], sorted(host["card_ids"])) == (True, 2, sorted(b_ids))
        assert (host["reading"], host["threshold"]) == (2, 2)
        assert host["detail"]["boards"] == {"default": 2, "beta": 0}  # per-board use of the host's slots
        with kb.scoped_current_board("beta"):
            for flags in ((), ("--live",)):
                first = _why(b_ids[0], *flags)["first_stop"]
                assert (first["governor"], first["reading"], first["threshold"]) == ("host_slots", 2, 2), flags
                assert first["detail"]["boards"] == {"default": 2, "beta": 0}
        assert _row(governors.snapshot(a), "host_slots")["cards"] == 0  # board A waits on nothing

        for tid in a_ids:  # board A finishes
            assert kb.complete_task(a, tid, summary="done", expected_run_id=kb.get_task(a, tid).current_run_id)
        _tick(b, started, max_in_progress=2, board="beta")
        assert sorted(started) == sorted(a_ids + b_ids)
        snap = governors.snapshot(b)
        _sums(snap)
        host = _row(snap, "host_slots")
        assert (host["active"], host["cards"], snap["idle_total"]) == (False, 0, 0)
        assert host["detail"]["boards"] == {"default": 0, "beta": 0}  # the census that admitted them

        out = json.loads(kcli.run_slash("governors --all-boards --json"))
        assert {s["board"] for s in out["boards"]} == {"default", "beta"}
        assert [r["id"] for r in out["registry"]] == [r["id"] for r in governors.registry()]


def test_a_card_two_governors_hold_is_charged_once_to_the_first_in_dispatcher_order(pool_hub, monkeypatch):
    """Acceptance 5: this machine cannot lease the card (labels_mismatch) AND its pool is at reserve."""
    pool_hub.home.joinpath("config.yaml").write_text(json.dumps({
        "kanban": {"sync": {"enabled": True}},
        "model_routing": {"enabled": True, "routes": {level: [{
            "model": model_tier.OPUS, "provider": "anthropic", "weight": 1, "billing": "subscription",
            "eligible": True, "funding_verified": True, "capabilities": {"tools": True, "streaming": True}}]
            for level in ("fast", "standard", "deep")}}}), encoding="utf-8")
    _usage(monkeypatch, [RESERVE + 10])
    # The overlap check has a per-tick time budget (next test); 16 parallel test workers are not what it bounds.
    monkeypatch.setattr(governors, "OVERLAP_BUDGET_SECONDS", 60.0)
    conn = pool_hub.conn
    both = [kb.create_task(conn, title=f"pool card {i}", assignee="default", runs_on=list(pool_wait.LABELS))
            for i in range(2)]
    paced = kb.create_task(conn, title="leasable here", assignee="default")
    result = pool_hub.tick()
    assert not result.spawned
    assert set(result.skipped_ineligible) == {(tid, "labels_mismatch") for tid in both}
    assert [p[0] for p in result.llm_paced] == [paced]

    snap = governors.snapshot(conn)
    _sums(snap)
    assert snap["idle_total"] == 3
    placement, pacing = _row(snap, "placement"), _row(snap, "llm_pool")
    assert (placement["cards"], sorted(placement["card_ids"])) == (2, sorted(both))
    assert (pacing["cards"], pacing["card_ids"]) == (1, [paced])  # the overlap is not charged a second time
    assert governors.order_of("placement") < governors.order_of("llm_pool")
    for tid in both:
        card = snap["cards"][tid]
        assert (card["governor"], card["detail"]["reason"]) == ("placement", "labels_mismatch")
        assert [(o["governor"], o.get("scope")) for o in card["overlaps"]] == [("llm_pool", None)]
        assert card["overlaps"][0]["reading"] == {"used_pct": RESERVE + 10, "accounts_under_reserve": 0, "accounts": 1}
        for flags in ((), ("--live",)):
            answer = _why(tid, *flags)
            assert answer["first_stop"]["governor"] == "placement", flags
            assert [o["governor"] for o in answer["overlaps"]] == ["llm_pool"], flags
    assert snap["cards"][paced]["overlaps"] == []


def test_overlaps_past_the_tick_budget_read_not_evaluated_and_fill_in_on_later_ticks(board, monkeypatch):
    """A tick spends a bounded time on overlaps: a card it did not reach says ``None``, never ``[]``."""
    _usage(monkeypatch, [10.0])
    monkeypatch.setattr(governors, "OVERLAP_BUDGET_SECONDS", 0.0)
    with kbc.connect() as conn:
        ids = [kb.create_task(conn, title=f"card {i}", assignee="default") for i in range(4)]
        _tick(conn, max_in_progress=1)
        waiting = [t for t in ids if kb.get_task(conn, t).status == "ready"]
        seen = []
        for _ in waiting:
            snap = governors.snapshot(conn)
            _sums(snap)  # the charge never waits for the overlap check
            assert {snap["cards"][t]["governor"] for t in waiting} == {"host_slots"}
            seen.append(sorted(str(snap["cards"][t]["overlaps"]) for t in waiting))
            texts = "".join(kcli.run_slash(f"why {t}") for t in waiting)
            assert ("not evaluated" in texts) == ("None" in seen[-1])
            _tick(conn, max_in_progress=1)
        assert seen == [["None", "None", "[]"], ["None", "[]", "[]"], ["[]", "[]", "[]"]]  # one more card a tick


def test_why_reads_the_snapshot_in_well_under_a_second_and_the_dry_run_writes_nothing(board, monkeypatch):
    """Acceptance 8, the local half: 300 idle cards behind one host slot."""
    _usage(monkeypatch, [10.0])
    with kbc.connect() as conn:
        ids = [kb.create_task(conn, title=f"card {i}", assignee="default") for i in range(300)]
        _tick(conn, max_in_progress=1)
        snap = governors.snapshot(conn)
        _sums(snap)
        assert _row(snap, "host_slots")["cards"] == 299 == snap["idle_total"]
        waiting = [t for t in ids if kb.get_task(conn, t).status == "ready"]

        started = time.monotonic()
        answer = gcli.why(conn, waiting[-1])
        assert time.monotonic() - started < 1.0
        assert (answer["source"], answer["first_stop"]["governor"]) == ("snapshot", "host_slots")

        before = (conn.execute("SELECT COUNT(*), MAX(id) FROM task_events").fetchone()[:],
                  conn.execute("SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (waiting[-1],)).fetchone()[:],
                  conn.total_changes)
        started = time.monotonic()
        live = explain.explain(conn, waiting[-1])
        assert time.monotonic() - started < 10.0
        assert live["first_stop"]["governor"] == "host_slots"
        assert {s["verdict"] for s in live["chain"]} <= {"pass", "stop", "throttle", "unknown"}
        assert before == (conn.execute("SELECT COUNT(*), MAX(id) FROM task_events").fetchone()[:],
                          conn.execute("SELECT status, claim_lock, assignee FROM tasks WHERE id = ?",
                                       (waiting[-1],)).fetchone()[:], conn.total_changes)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 0  # the walk's read-only guard is released


def test_no_snapshot_and_a_stale_one_read_as_unknown_not_as_nothing_held(board):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="never ticked", assignee="default")
        snap = governors.snapshot(conn)
        assert (snap["available"], snap["stale"], snap["idle_total"]) == (False, True, None)
        assert {r["id"] for r in governors.registry()} == {r["governor"] for r in snap["governors"]}
        assert "no data" in kcli.run_slash("governors")
        assert _why(tid)["source"] == "dry_run"  # nothing recorded: asked live instead of answered "not held"

        _tick(conn, max_in_progress=1, dry_run=True)  # a dry run decides nothing: no snapshot
        assert not governors.snapshot(conn)["available"]
        other = kb.create_task(conn, title="second", assignee="default")
        _tick(conn, max_in_progress=1)
        assert governors.snapshot(conn, now=time.time() + governors.STALE_SECONDS + 5)["stale"]
        waiting = tid if kb.get_task(conn, tid).status == "ready" else other
        assert governors.card(conn, waiting)["governor"] == "host_slots"
        assert governors.card(conn, waiting, now=time.time() + governors.STALE_SECONDS + 5)["stale"]


def _chain_step(answer, governor):
    return next(step for step in answer["chain"] if step["governor"] == governor)


def _printed_verdict(text, label):
    """The verdict token on the dry-run chain line for ``label``, or None."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"pass", "stop", "throttle", "unknown"} and label in line:
            return parts[0]
    return None


def test_never_ticked_unknown_does_not_say_nothing_holds_it(board, monkeypatch, capsys):
    """A card no tick has judged is unknown, not 'nothing holds it'. Missing and stale text too."""
    _usage(monkeypatch, [10.0])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="not yet observed", assignee="default")
        answer = gcli.why(conn, tid)
        gcli._print_why(answer)
        text = capsys.readouterr().out
        unknown = [step["governor"] for step in answer["chain"] if step["verdict"] == "unknown"]
        assert {"board_share", "hub_claim_screen", "admission_pool", "board_lease"} <= set(unknown), unknown
        assert "nothing holds it" not in text, text
        assert "unknown whether anything holds it" in text, text
        slash = kcli.run_slash(f"why {tid}")
        assert "nothing holds it" not in slash, slash
        assert "unknown whether anything holds it" in slash, slash

        _tick(conn, max_in_progress=1)
        other = kb.create_task(conn, title="second", assignee="default")
        _tick(conn, max_in_progress=1)
        first = kb.get_task(conn, tid)
        waiting = tid if first is not None and first.status == "ready" else other
        stale_at = time.time() + governors.STALE_SECONDS + 5
        recorded = governors.card(conn, waiting, now=stale_at)
        assert recorded is not None and recorded["stale"]
        monkeypatch.setattr(time, "time", lambda: stale_at)
        stale = gcli.why(conn, waiting)
        stale_text = kcli.run_slash(f"why {waiting}")
        assert stale["source"] == "dry_run"
        lease = _chain_step(stale, "board_lease")
        assert lease["verdict"] == "unknown", lease
        assert _printed_verdict(stale_text, "Board lease") == "unknown", stale_text
        if stale.get("first_stop") is None:
            assert "nothing holds it" not in stale_text, stale_text
            assert "unknown whether anything holds it" in stale_text, stale_text


def test_unreached_tick_only_gate_is_unknown(board, monkeypatch):
    """A tick that stops before claim did not pass the Board lease; absence is not a pass."""
    _usage(monkeypatch, [10.0])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="globally stopped", assignee="default")
        _tick(conn, max_spawn=0)
        answer = gcli.why(conn, tid, live=True)
        lease = _chain_step(answer, "board_lease")
        assert lease["verdict"] == "unknown", lease
        assert lease["verdict"] != "pass"


def test_refresh_false_alternate_does_not_read_usage(board, monkeypatch):
    """A read-only why must not refresh an alternate pool or rewrite pacing state."""
    cfg = json.loads(board.joinpath("config.yaml").read_text())
    for routes in cfg["model_routing"]["routes"].values():
        routes.append({**routes[0], "model": "gpt-6-astra", "provider": "openai-codex"})
    board.joinpath("config.yaml").write_text(json.dumps(cfg))
    from hermes_cli import kanban_provider_walls as pw
    monkeypatch.setattr(pw, "live_pool_serving", lambda *a: True)
    monkeypatch.setattr(pw, "live_quota_wall", lambda *a: None)
    reads = []

    def read_pool(provider, now):
        reads.append(provider)
        used = 99.0 if provider == "anthropic" else 10.0
        return [lp.Reading(now, provider + "-acct", lp.SEVEN_DAY, used, now + 86400)]

    monkeypatch.setattr(pd, "read_pool", read_pool)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="two eligible pools", assignee="default")
        pd.pace_for(conn, "anthropic", pd.load_config(), time.time())
        reads.clear()
        before = pd.state_path().read_bytes()
        gcli.why(conn, tid, live=True)
        after = pd.state_path().read_bytes()
        assert reads == [] and after == before, {"reads": reads, "state_changed": after != before}


def test_later_global_stop_is_an_overlap_on_the_snapshot_and_live(board, monkeypatch):
    """A global rule after the tick's first stop is an overlap, not an evaluated empty list.

    ``dispatch_once(max_spawn=0)`` returns before it reads memory. Critical pressure is still a
    stop; snapshot ``why`` must name it, the same way ``why --live`` does.
    """
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "critical")
    monkeypatch.setattr(governors, "OVERLAP_BUDGET_SECONDS", 60.0)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="spawn cap and critical memory", assignee="default")
        result = _tick(conn, max_spawn=0)
        task = kb.get_task(conn, tid)
        assert not result.spawned and task is not None and task.status == "ready"
        card = governors.snapshot(conn)["cards"][tid]
        assert card["governor"] == "max_spawn", card
        snap = _why(tid)
        live = _why(tid, "--live")
        assert snap["source"] == "snapshot" and live["source"] == "dry_run"
        assert snap["first_stop"]["governor"] == "max_spawn"
        assert "memory_pressure" in [o["governor"] for o in live["overlaps"]], live["overlaps"]
        assert "memory_pressure" in [o["governor"] for o in snap["overlaps"]], snap["overlaps"]
        assert "memory_pressure" in [o["governor"] for o in card["overlaps"]], card


def test_predicate_fault_is_unknown_not_a_cached_empty_overlap_list(board, monkeypatch):
    """An overlap predicate that raises is unknown, and that incomplete result is not reused."""
    from hermes_cli import kanban_disk_admission as disk

    monkeypatch.setattr(governors, "OVERLAP_BUDGET_SECONDS", 60.0)

    def boom():
        raise OSError("probe")

    monkeypatch.setattr(disk, "refusal_reason", boom)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="disk read fails", assignee="default")
        _tick(conn, max_spawn=0)
        card = governors.snapshot(conn)["cards"][tid]
        assert card["governor"] == "max_spawn", card
        unknowns = card.get("overlaps_unknown") or []
        assert any(u["governor"] == "disk_floor" and u["verdict"] == "unknown" for u in unknowns), card
        assert "overlaps_at" not in card  # a fault is not a finished evaluation to reuse for 300s
        snap = _why(tid)
        assert any(u["governor"] == "disk_floor" for u in (snap.get("overlaps_unknown") or [])), snap
        text = kcli.run_slash(f"why {tid}")
        assert "also unknown" in text and "Disk floor" in text, text
        assert "not evaluated yet" not in text, text
        live = _why(tid, "--live")
        assert _chain_step(live, "disk_floor")["verdict"] == "unknown"

        _tick(conn, max_spawn=0)  # still faulting: must not reuse a cached empty list
        again = governors.snapshot(conn)["cards"][tid]
        assert any(u["governor"] == "disk_floor" for u in (again.get("overlaps_unknown") or [])), again

        monkeypatch.setattr(disk, "refusal_reason", lambda: None)
        _tick(conn, max_spawn=0)
        recovered = governors.snapshot(conn)["cards"][tid]
        assert not any(u.get("governor") == "disk_floor" for u in (recovered.get("overlaps_unknown") or []))
        assert "Disk floor" not in kcli.run_slash(f"why {tid}")
