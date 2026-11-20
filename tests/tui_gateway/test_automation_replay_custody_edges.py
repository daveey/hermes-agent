"""Custody survives empty passes, absent stores and a second backend retirement."""
from types import SimpleNamespace

from tests.tui_gateway import test_automation_replay_handoff as helpers
from tui_gateway import automation_replay as ar

wired = helpers.wired
journal = helpers.journal
KEY = helpers.KEY


def test_empty_consumer_pass_releases_finished_worker_references(wired, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    held = {"finished": SimpleNamespace(alive=lambda: False),
            "live": SimpleNamespace(alive=lambda: True)}
    monkeypatch.setattr(ar, "_deliveries", held)
    assert ar.consume_handoffs([], now=100) == 0
    assert set(held) == {"live"}


def test_stop_without_session_store_still_cancels_local_unpersisted(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    home = tmp_path / "missing-store"
    home.mkdir()
    ar.schedule(KEY, text="report", display_kind="process_complete", display_metadata={},
                due=200, cause="build", persisted=False, now=100, profile_home=home)
    ar.cancel_durable(home, (KEY,), 150, stop=True)
    assert not ar.pending(KEY, profile_home=home)
    assert not (home / "state.db").exists()


def test_second_retirement_preserves_rewall_batch_provenance(wired, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    marker = {"automation_replay": {"attempt": 1, "failed_at": 10, "batch_id": "first-batch"}}
    state = SimpleNamespace(automation_plan=ar.Plan(True, 200, "retry", "quota"),
                            automation_text="report", display_kind="process_complete", display_metadata=marker)
    ar.schedule_for_turn({"session_key": KEY, "profile_home": wired.path.parent}, state)
    assert ar.handoff(KEY)
    batch = wired.claim(KEY, 1e20, "second-backend", lambda _: False)
    import json
    recovered = ar._item_from(json.loads(batch["payload"])[0])
    assert recovered.source_batch == "first-batch"
    assert recovered.attempt == 1 and recovered.failed_at == 10 and recovered.persisted


def test_error_budget_is_not_reset_by_consumer_process_replacement(wired, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    wired.publish(KEY, [helpers.item(due=0)])
    calls = []
    for _ in range(ar.HANDOFF_FAILURES_MAX + 2):
        # A new backend has no predecessor's module-local error counters.
        getattr(ar, "_failures", {}).clear()
        ar.consume_handoffs([wired.path.parent], now=5000,
                            wake=lambda *a, **kw: calls.append(1) or "error")
    assert len(calls) == ar.HANDOFF_FAILURES_MAX
    assert wired.owners() == []


def test_inflight_local_join_is_not_consumed_or_armed_twice(wired, monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    permits = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **kw:
                        permits.append(kw["replay_delivery"]) or "started")
    helpers._arm_mixed(wired.path.parent)
    ar._fire(KEY)
    ar.schedule(KEY, text="later", display_kind="process_complete", display_metadata={},
                due=9000, cause="quota", persisted=False, now=2000, profile_home=wired.path.parent)
    ar._fire(KEY)
    assert len(permits) == 1
    assert permits[0].admit()
    entry = ar._entries[(str(wired.path.parent), KEY)]
    assert [i.text for i in entry.items] == ["later"] and entry.due == 9000
    assert entry.fire_token is None
    assert not permits[0].admit()


def test_transfer_in_progress_fences_local_admission_and_stop_fences_late_publish(wired, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tui_gateway import server
    from tui_gateway.automation_replay_handoff import Journal
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    permits = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **kw:
                        permits.append(kw["replay_delivery"]) or "started")
    helpers._arm_mixed(wired.path.parent)
    ar._fire(KEY)
    entered, release = threading.Event(), threading.Event()
    original = Journal.publish
    def delayed(self, owner, items):
        entered.set()
        assert release.wait(10)
        return original(self, owner, items)
    monkeypatch.setattr(Journal, "publish", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        transferred = pool.submit(ar.handoff, KEY)
        try:
            assert entered.wait(10)
            assert not permits[0].admit()
            ar.cancel_durable(wired.path.parent, (KEY,), 5000, stop=True)
        finally:
            release.set()
        assert transferred.result(timeout=10)
    assert not ar.pending(KEY) and wired.owners() == []
    assert not permits[0].admit()


def test_failed_transfer_rearms_without_spending_local_busy_budget(wired, monkeypatch):
    from tui_gateway import server
    from tui_gateway.automation_replay_handoff import Journal
    import sqlite3
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    permits = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **kw:
                        permits.append(kw["replay_delivery"]) or "started")
    helpers._arm_mixed(wired.path.parent)
    ar._fire(KEY)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "retired")
    def locked(*args):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(Journal, "publish", locked)
    assert not permits[0].admit()
    entry = ar._entries[(str(wired.path.parent), KEY)]
    assert entry.fire_token is None and not entry.transferring and entry.busy_rechecks == 0
    assert len(entry.items) == 2


def test_durable_error_cap_survives_death_before_final_discard(wired, monkeypatch):
    from tui_gateway.automation_replay_handoff import Journal
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    wired.publish(KEY, [helpers.item(due=0)])
    batch = wired.claim(KEY, 5000, "dead-consumer", lambda _: False)
    for _ in range(ar.HANDOFF_FAILURES_MAX):
        assert wired.record_failure(batch)
    assert not Journal(wired.path.parent).record_failure({**batch, "token": "stale"})
    calls = []
    ar.consume_handoffs([wired.path.parent], wake=lambda *a, **kw: calls.append(1) or "started", now=5000)
    assert calls == [] and wired.owners() == []


def test_old_local_worker_cannot_enter_after_replacement(wired, monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    permits = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **kw:
                        permits.append(kw["replay_delivery"]) or "started")
    helpers._arm_mixed(wired.path.parent)
    ar._fire(KEY)
    ar.reset()  # the old entry is detached, but its delayed worker still has its snapshot
    helpers._arm_mixed(wired.path.parent, now=9000)
    assert not permits[0].admit()
    replacement = ar._entries[(str(wired.path.parent), KEY)]
    assert len(replacement.items) == 2 and replacement.fire_token is None


def test_local_busy_recheck_respects_a_reset_joined_during_dispatch(wired, monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    monkeypatch.setattr(ar.time, "time", lambda: 1000)
    helpers._arm_mixed(wired.path.parent)
    delays = []
    original = ar._arm
    def record(key, entry, delay, fire):
        delays.append(delay)
        return original(key, entry, delay, fire)
    monkeypatch.setattr(ar, "_arm", record)
    def wake(*a, **kw):
        ar.schedule(KEY, text="later wall", display_kind="process_complete", display_metadata={},
                    due=9000, cause="quota", persisted=False, now=1000, profile_home=wired.path.parent)
        return "busy"
    monkeypatch.setattr(server, "_wake_session_turn", wake)
    ar._fire(KEY)
    assert delays == [8000]


def test_retired_local_worker_without_resolvable_home_cannot_loop(wired, monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(ar, "_home_of", lambda _: None)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    permits = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **kw:
                        permits.append(kw["replay_delivery"]) or "started")
    helpers._arm_mixed(None)
    ar._fire(KEY)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "retired")
    assert not permits[0].admit()
    assert not ar.pending(KEY)
