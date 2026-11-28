"""Automatic retirement must prove a held card moot and never release work or discard human custody."""
import json
import sys
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_lanes as lanes, kanban_lanes_archive as arc
from hermes_cli import kanban_lanes_plan as plan
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person

OLD = 9 * 86400
SHA = "17f725e055a7bd16c71812aea1c91de61273823d"


def held(conn, *, creator="core-ship-backstop", key=SHA, age=OLD, title="Retro review", source=None):
    card = kb.create_task(conn, title=title, assignee="default", created_by=creator, tenant="hermes",
                          idempotency_key=key)
    stamp = int(time.time()) - age
    conn.execute("UPDATE tasks SET status='triage', block_kind=?, created_at=? WHERE id=?",
                 (lanes.GATE_KIND, stamp, card))
    if source is not None:
        conn.execute("UPDATE task_events SET payload=json_set(COALESCE(payload,'{}'),'$.creator_source',json(?)) "
                     "WHERE task_id=? AND kind='created'", (json.dumps(source), card))
    kb._append_event(conn, card, lanes.GATED, {"lane": "hermes"})
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?", (stamp, card, lanes.GATED))
    conn.commit()
    return card


def archive(conn, card, dry_run=False, **extra):
    action = {"action": "archive", "card": card, "basis": "stale", "reason": "moot", **extra}
    return plan.apply(conn, [action], dry_run=dry_run)


def archived_event(conn, card):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='archived'", (card,)).fetchone()
    return None if row is None else json.loads(row[0])


def status(conn, card):
    return kb.get_task(conn, card).status


def test_stale_informational_archive_has_audited_basis_and_no_admission(conn, configured):
    card = held(conn)
    result = archive(conn, card)
    assert result["applied"], result
    assert status(conn, card) == "archived"
    event = archived_event(conn, card)
    assert event["basis"] == "stale" and event["reason"] == "moot" and event["via"] == "lane-planner"
    assert lanes.admitted_today(conn, "hermes") == 0


def test_daily_flaky_intake_is_stale_after_its_day_only(conn, configured):
    old = held(conn, creator="gw:hub", key="ci-flaky-intake:o/r:2026-01-02:abc", age=60)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    fresh = held(conn, creator="gw:hub", key=f"ci-flaky-intake:o/r:{today}:abc", age=60)
    result = plan.apply(conn, [{"action": "archive", "card": c, "basis": "stale", "reason": "old"}
                               for c in (old, fresh)])
    assert [a["card"] for a in result["applied"]] == [old]
    assert result["rejected"][0]["why"] == "daily intake is from today"


def test_target_done_archives_alarm_only_when_its_target_finished(conn, configured):
    target = kb.create_task(conn, title="work", assignee="default", created_by="user")
    card = held(conn, creator="scope-renewal-alarm", key=f"scope-renewal-loop:{target}:01a2", age=60)
    first = archive(conn, card, basis="target_done")
    assert first["rejected"][0]["why"].startswith(f"target card {target} is still"), first
    assert archive(conn, held(conn), basis="target_done")["rejected"][0]["why"] == \
        "this producer's cards name no target card"
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (target,))
    conn.commit()
    assert archive(conn, card, basis="target_done")["applied"]
    assert archived_event(conn, card)["basis"] == "target_done"


def test_superseded_needs_newer_open_card_from_same_producer_and_subject(conn, configured):
    older = held(conn, creator="worker-core-rollout", key="worker-core-rollout:lag:worker-1:20261119", age=OLD)
    other = held(conn, creator="worker-core-rollout", key="worker-core-rollout:lag:worker-2:20261120", age=60)
    newer = held(conn, creator="worker-core-rollout", key="worker-core-rollout:lag:worker-1:20261127", age=60)
    assert archive(conn, older, basis="superseded", by=other)["rejected"][0]["why"].startswith("the newer card is not")
    assert archive(conn, newer, basis="superseded", by=older)["rejected"][0]["why"] == "the 'by' card is not newer"
    assert archive(conn, older, basis="superseded")["rejected"][0]["why"] == "superseded needs 'by': another card"
    assert archive(conn, older, basis="superseded", by=newer)["applied"]
    assert archived_event(conn, older)["by"] == newer
    assert status(conn, newer) == "triage"


def _checker(tmp_path, state, code=0):
    script = tmp_path / f"check_{state}_{code}.py"
    script.write_text("import json, sys\n"
                      f"print(json.dumps({{'state': {state!r}, 'evidence': 'card ' + sys.argv[1]}}))\n"
                      f"sys.exit({code})\n")
    return [sys.executable, str(script)]


@pytest.mark.parametrize("state,code,ok", [("clear", 0, True), ("active", 0, False), ("clear", 3, False),
                                           ("bogus", 0, False), (None, 0, False)])
def test_condition_cleared_uses_only_the_operator_registered_check(conn, configured, tmp_path, state, code, ok):
    card = held(conn, creator="ci-main-watch", key=f"ci-red:daveey/hermes:{SHA}#4", age=60)
    if state is not None:
        configured(archive_checks={"ci-main-watch": _checker(tmp_path, state, code)})
    result = archive(conn, card, basis="condition_cleared", check=["/bin/true"])  # a plan's command is ignored
    assert bool(result["applied"]) is ok, result
    assert (status(conn, card) == "archived") is ok
    if ok:
        assert archived_event(conn, card)["condition"] == {"state": "clear", "evidence": f"card {card}"}


def test_condition_check_is_rebound_to_the_key_inside_the_write(conn, configured, tmp_path, monkeypatch):
    card = held(conn, creator="ci-main-watch", key=f"ci-red:daveey/hermes:{SHA}", age=60)
    configured(archive_checks={"ci-main-watch": _checker(tmp_path, "clear")})
    real = arc.run_check

    def racing(prod, row):  # the card is re-keyed for a new head after the check ran
        out = real(prod, row)
        conn.execute("UPDATE tasks SET idempotency_key=? WHERE id=?", (f"ci-red:daveey/hermes:{'a' * 40}", card))
        conn.commit()
        return out

    monkeypatch.setattr(arc, "run_check", racing)
    result = archive(conn, card, basis="condition_cleared")
    assert result["rejected"][0]["why"] == "the condition was not checked for this card"
    assert status(conn, card) == "triage"


def test_guard_rechecks_inside_the_archive_transaction(conn, configured, monkeypatch):
    card = held(conn)
    real = arc.refusal

    def claim_first(c, task_id, action, **kw):  # a worker claims inside the window before the guarded write
        c.execute("UPDATE tasks SET claim_lock='worker:1' WHERE id=?", (task_id,))
        return real(c, task_id, action, **kw)

    monkeypatch.setattr(arc, "refusal", claim_first)
    assert archive(conn, card)["rejected"][0]["why"] == "card has execution custody"
    assert status(conn, card) == "triage"


def test_dry_run_reports_the_verdict_without_writing(conn, configured):
    moot, fresh = held(conn), held(conn, key="a" * 40, age=60)
    result = plan.apply(conn, [{"action": "archive", "card": c, "basis": "stale", "reason": "r"}
                               for c in (moot, fresh)], dry_run=True)
    assert [a["card"] for a in result["applied"]] == [moot]
    assert "seven days" in result["rejected"][0]["why"]
    assert status(conn, moot) == "triage" and archived_event(conn, moot) is None


def test_producer_withdraw_is_own_cards_only_and_not_a_plan_basis(conn, configured):
    card = held(conn, creator="ci-main-watch", key=f"ci-red:daveey/hermes:{SHA}", age=60)
    assert archive(conn, card, basis="producer_withdrawn", producer="ci-main-watch")["rejected"]
    assert arc.withdraw(conn, card, producer="worker-core-rollout", reason="x") == \
        "only the producer that filed the card withdraws it"
    assert arc.withdraw(conn, card, producer="ci-main-watch", reason="main green twice") is None
    event = archived_event(conn, card)
    assert event["basis"] == "producer_withdrawn" and event["via"] == "producer:ci-main-watch"


def test_held_listing_carries_typed_producer_hints(conn, configured):
    target = kb.create_task(conn, title="work", assignee="default", created_by="user")
    card = held(conn, creator="scope-renewal-alarm", key=f"scope-renewal-loop:{target}:01a2")
    rows = {r["id"]: arc.hint(r) for r in lanes.held(conn)}
    assert rows[card] == {"producer": "scope-renewal-alarm", "informational": False, "target": target}


GUARDS = {"human": "catalogued", "chat": "chat session", "uncatalogued_key": "catalogued", "running": "not held",
          "child": "children", "ask": "human request", "claimed": "custody", "live_run": "live run",
          "recent": "seven days", "repair_work": "not an informational", "basis": "basis must be",
          "gate_recent": "seven days", "gate_missing": "seven days", "admitted": "not held",
          "replica": "board's home", "contained": "containment"}


@pytest.mark.parametrize("guard", sorted(GUARDS))
def test_archive_refuses_protected_or_unproved_cards(conn, configured, guard, monkeypatch):
    card = held(conn, creator="user" if guard == "human" else "core-ship-backstop",
                key="core-ship-backstop:abc" if guard == "uncatalogued_key" else SHA,
                source={"author": "default", "session_id": "20261128_041617_d23541"} if guard == "chat" else None)
    updates = {
        "running": ("status", "running"),
        "ask": ("human_request", '{"action":"Decide"}'),
        "claimed": ("claim_lock", "worker:123"),
        "recent": ("created_at", int(time.time())),
        "repair_work": ("created_by", "ci-main-watch"),
    }
    if guard in updates:
        column, value = updates[guard]
        conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, card))
    if guard == "repair_work":  # real repair work from a catalogued producer is never merely "stale"
        conn.execute("UPDATE tasks SET idempotency_key=? WHERE id=?", (f"ci-red:daveey/hermes:{SHA}", card))
    if guard == "gate_recent":
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?",
                     (int(time.time()), card, lanes.GATED))
    if guard == "gate_missing":
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (card, lanes.GATED))
    conn.commit()
    if guard == "child":
        child = kb.create_task(conn, title="waiting implementation", assignee="default", created_by="user")
        kb.link_tasks(conn, card, child)
        monkeypatch.setattr(lanes, "HELD", "1=1")  # linking may also move the parent; prove the child check itself
    if guard == "live_run":
        monkeypatch.setattr(lanes, "HELD", "1=1")  # a run row also drops it from HELD; prove the guard's own check
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'default', 'running', ?)",
                     (card, int(time.time())))
        conn.commit()
    if guard == "admitted":
        kb._append_event(conn, card, "lane_admitted", {})
        conn.commit()
    if guard == "replica":
        monkeypatch.setattr("hermes_cli.kanban_db_sync.sync_state", lambda _c: {"gateway_id": "worker-a2"})
    if guard == "contained":
        monkeypatch.setattr("hermes_cli.kanban_containment_guard.pending", lambda _c, _t, **_k: True)
    extra = {"basis": "because I said so"} if guard == "basis" else {}
    before = status(conn, card)
    result = archive(conn, card, **extra)
    assert not result["applied"] and result["rejected"], result
    assert GUARDS[guard] in result["rejected"][0]["why"], result
    assert status(conn, card) == before
    assert archived_event(conn, card) is None
    assert not archive(conn, card, dry_run=True, **extra)["applied"]
