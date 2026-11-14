"""Reader eviction registration must not own retired plugin instances."""
import gc
import sqlite3
import weakref

from hermes_cli import kanban_generation as kg


class Connection(sqlite3.Connection):
    """sqlite3's subclass supports weak references for lifetime assertions."""


class Cache:
    def __init__(self, path):
        self.conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, factory=Connection)

    def evict(self, path):
        self.conn.close()


def test_retired_caches_release_five_readonly_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(kg, "_reader_caches", type(kg._reader_caches)())
    path = tmp_path / "board.db"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE tasks (id TEXT)")
    writer.close()
    caches = [Cache(path) for _ in range(5)]
    connections = [weakref.ref(cache.conn) for cache in caches]
    for cache in caches:
        kg.register_reader_cache(cache.evict)
    assert len(kg._reader_caches) == 5
    del cache, caches
    gc.collect()
    assert len(kg._reader_caches) == 0
    assert all(ref() is None for ref in connections)


def test_unregister_removes_only_matching_entry_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(kg, "_reader_caches", type(kg._reader_caches)())

    def first(path):
        pass

    def second(path):
        pass

    kg.register_reader_cache(first)
    kg.register_reader_cache(second)
    kg.register_reader_cache(first)
    assert len(kg._reader_caches) == 2
    assert kg.unregister_reader_cache(first) is True
    assert list(kg._reader_caches) == [second]
    assert kg.unregister_reader_cache(first) is False
    assert list(kg._reader_caches) == [second]


def test_recover_calls_live_cache_outside_registry_lock(tmp_path, monkeypatch):
    import threading
    from hermes_cli import kanban_lockguard

    monkeypatch.setattr(kg, "_reader_caches", type(kg._reader_caches)())
    monkeypatch.setattr(kg, "_connections", weakref.WeakSet())
    monkeypatch.setattr(kg, "_poisoned", set())
    path = str(tmp_path / "board.db")
    calls = []
    scans = iter([["retired-wal"], []])
    monkeypatch.setattr(kanban_lockguard, "stranded_sidecars", lambda _: next(scans))

    class LiveCache:
        def evict(self, board):
            def register_from_other_thread():
                with kg._lock:
                    calls.append(board)
            thread = threading.Thread(target=register_from_other_thread)
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive(), "recovery held registry lock across eviction"

    cache = LiveCache()
    kg.register_reader_cache(cache.evict)
    kg.register_reader_cache(cache.evict)
    assert len(kg._reader_caches) == 1
    kg.recover(path)
    assert calls == [path]
    assert kg.unregister_reader_cache(cache.evict) is True
    assert kg.unregister_reader_cache(cache.evict) is False


def test_function_lifetime_and_registration_prune_dead_entries(monkeypatch):
    monkeypatch.setattr(kg, "_reader_caches", type(kg._reader_caches)())

    def callback(path):
        pass

    kg.register_reader_cache(callback)
    assert callback in kg._reader_caches
    ref = weakref.ref(callback)
    del callback
    gc.collect()
    assert ref() is None

    def replacement(path):
        pass

    kg.register_reader_cache(replacement)
    assert list(kg._reader_caches) == [replacement]
    assert len(kg._reader_caches._entries) == 1


def test_non_weakrefable_callable_is_retained_until_unregistered(monkeypatch):
    monkeypatch.setattr(kg, "_reader_caches", type(kg._reader_caches)())

    class Callback:
        __slots__ = ()

        def __call__(self, path):
            pass

    callback = Callback()
    kg.register_reader_cache(callback)
    gc.collect()
    assert list(kg._reader_caches) == [callback]
    assert kg.unregister_reader_cache(callback) is True
    assert len(kg._reader_caches) == 0
