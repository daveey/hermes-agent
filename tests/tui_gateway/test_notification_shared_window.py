"""Structural sharing must not share chat custody or clock decisions."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from tui_gateway import kanban_remote_index as published
from tui_gateway import kanban_remote_notices as notices
from tui_gateway import kanban_replica_observer as observer
from tui_gateway import notification_shared_window as shared


@pytest.fixture(autouse=True)
def window(monkeypatch):
    shared.reset()
    monkeypatch.delenv("HERMES_NOTIFICATION_SHARED_WINDOW", raising=False)
    clock = [100.0]
    monkeypatch.setattr(shared, "_clock", lambda: clock[0])
    monkeypatch.setattr(observer, "fingerprint", Mock(return_value=("stamp",)))
    monkeypatch.setattr(published, "lookup", Mock(return_value=None))
    monkeypatch.setattr(published, "current_key", Mock(return_value="key"))
    monkeypatch.setattr(published, "compute", Mock(return_value=("local", {
        ("default", "a"): [{"task": {"id": "one"}}],
        ("default", "b"): [{"task": {"id": "two"}}],
        ("other", "a"): [{"task": {"id": "three"}}],
    }, {})))
    yield clock
    shared.reset()


def read(tmp_path, sid="a", profile="default", ledger="ledger.db", hub="hub.db"):
    return notices._chat_index(tmp_path / hub, tmp_path / ledger, 1000.0, profile, (sid,), shared_window=True)


def test_default_on_shares_across_chats_profiles_and_expires(tmp_path, window):
    assert read(tmp_path)[1]["a"][0]["task"]["id"] == "one"
    assert read(tmp_path, "b")[1]["b"][0]["task"]["id"] == "two"
    assert read(tmp_path, profile="other")[1]["a"][0]["task"]["id"] == "three"
    assert observer.fingerprint.call_count == published.compute.call_count == 1
    window[0] = 101.999
    read(tmp_path)
    assert published.compute.call_count == 1
    window[0] = 102.0
    read(tmp_path)
    assert observer.fingerprint.call_count == published.compute.call_count == 2


def test_off_is_literal_and_reenable_is_cold(tmp_path, monkeypatch):
    read(tmp_path)
    monkeypatch.setenv("HERMES_NOTIFICATION_SHARED_WINDOW", "0")
    read(tmp_path)
    read(tmp_path)
    assert observer.fingerprint.call_count == published.compute.call_count == 3
    monkeypatch.delenv("HERMES_NOTIFICATION_SHARED_WINDOW")
    read(tmp_path)
    assert published.compute.call_count == 4


def test_scope_includes_replica_and_observation_ledger(tmp_path):
    read(tmp_path)
    read(tmp_path, ledger="second.db")
    read(tmp_path, hub="other.db")
    assert published.compute.call_count == observer.fingerprint.call_count == 3


def test_concurrent_cold_pollers_build_once(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = published.compute

    def compute(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(published, "compute", compute)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(read, tmp_path)
        assert entered.wait(10)
        # Observe every contender reaching the real lock, not merely a start barrier.
        lock = shared._lock
        reached = threading.Condition()
        attempts = [0]

        class ObservedLock:
            def __enter__(self):
                with reached:
                    attempts[0] += 1
                    reached.notify_all()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        monkeypatch.setattr(shared, "_lock", ObservedLock())
        others = [pool.submit(read, tmp_path, "b") for _ in range(7)]
        try:
            with reached:
                assert reached.wait_for(lambda: attempts[0] == 7, timeout=10)
        finally:
            release.set()
        assert first.result()[1]["a"]
        assert all(f.result()[1]["b"] for f in others)
    assert original.call_count == observer.fingerprint.call_count == 1


def test_errors_unknown_and_backward_clock_retry(tmp_path, window):
    published.compute.side_effect = RuntimeError("retry")
    with pytest.raises(RuntimeError, match="retry"):
        read(tmp_path)
    published.compute.side_effect = None
    read(tmp_path)
    assert observer.fingerprint.call_count == 2
    window[0] = 99.0
    read(tmp_path)
    assert published.compute.call_count == 3
    observer.fingerprint.return_value = None
    shared.reset()
    read(tmp_path)
    read(tmp_path)
    assert published.compute.call_count == 5


def test_defer_stays_per_chat_and_is_not_a_cached_empty_result(tmp_path, monkeypatch):
    monkeypatch.setattr(published, "defer", Mock(side_effect=[True, False]))
    deferred = Mock()
    assert notices._chat_index(tmp_path / "hub.db", tmp_path / "ledger.db", 1000,
                               "default", ("a",), own=True, on_defer=deferred, shared_window=True) == ("", {}, {})
    assert notices._chat_index(tmp_path / "hub.db", tmp_path / "ledger.db", 1000,
                               "default", ("b",), own=True, shared_window=True)[1]["b"]
    assert deferred.call_count == 1
    assert published.defer.call_count == 2
    assert published.compute.call_count == 1


def test_collect_rechecks_claim_and_ack_inside_window(tmp_path, monkeypatch):
    """Use real collect/ledger: sharing candidates cannot cache a delivery verdict."""
    (tmp_path / "hub.db").touch()
    task = {"id": "one", "title": "proof", "status": "blocked", "run": {"ended_at": 1000}}
    card = {"gw": "remote", "key": "default/default/one", "task": task}
    published.compute.return_value = ("local", {("default", "a"): [card],
                                               ("other", "b"): [card]}, {})
    monkeypatch.setattr(notices, "_my_holder", lambda: (123, 456))
    monkeypatch.setattr(notices, "_holder_is_me", lambda pid: pid == 123)
    monkeypatch.setattr(notices, "_holder_alive", lambda pid, start: pid == 123)
    monkeypatch.setattr(notices, "_notice_item", lambda c, sid, *args: {
        "session": sid, "ref": "remote/default/default/one", "state": "blocked@1000", "text": "proof"})
    monkeypatch.setattr(notices, "_note_poller_claim", lambda *args: None)
    from tui_gateway import kanban_remote_late
    monkeypatch.setattr(kanban_remote_late, "collect_groups", lambda items, now: items)
    kwargs = {"hub_db": tmp_path / "hub.db", "ledger": tmp_path / "ledger.db", "now": 1001,
              "shared_window": True}
    offered = notices.collect("a", "default", **kwargs)
    assert len(offered) == 1
    assert notices.collect("a", "default", **kwargs) == []  # held, not reoffered
    assert notices.release(offered[0], ledger=kwargs["ledger"])
    offered = notices.collect("a", "default", **kwargs)
    assert len(offered) == 1  # release takes effect without a structural refresh
    assert notices.ack(offered[0], ledger=kwargs["ledger"])
    assert notices.collect("a", "default", **kwargs) == []
    assert len(notices.collect("b", "other", **kwargs)) == 1
    assert published.compute.call_count == observer.fingerprint.call_count == 1


def test_bounded_scopes_reset_and_slow_build_do_not_extend_ttl(tmp_path, monkeypatch, window):
    monkeypatch.setattr(shared, "_MAX_SCOPES", 2)
    read(tmp_path)
    read(tmp_path, ledger="second.db")
    read(tmp_path, ledger="third.db")
    assert len(shared._entries) == 2
    read(tmp_path)
    assert published.compute.call_count == 4  # first scope was evicted
    notices._reset_for_tests()
    read(tmp_path)
    assert published.compute.call_count == 5
    shared.reset()

    def slow(*args):
        window[0] += 3.0
        return "local", {}, {}

    published.compute.side_effect = slow
    read(tmp_path)
    read(tmp_path)
    assert published.compute.call_count == 7  # age starts before expensive work


def test_nonpoller_keeps_immediate_literal_reads(tmp_path, window):
    notices._chat_index(tmp_path / "hub.db", tmp_path / "ledger.db", 1000, "default", ("a",))
    notices._chat_index(tmp_path / "hub.db", tmp_path / "ledger.db", 1000, "default", ("a",))
    assert published.compute.call_count == observer.fingerprint.call_count == 2
