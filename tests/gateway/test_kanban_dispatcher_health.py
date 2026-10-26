"""Ongoing dispatcher receipts are not singleton-lock or one-shot evidence."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import kanban_dispatcher_health as health
from gateway.kanban_watchers_common import _acquire_singleton_lock, _release_singleton_lock

pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def owner(tmp_path):
    lock, state = _acquire_singleton_lock(tmp_path / ".dispatcher.lock")
    assert state == "held"
    producer = health.DispatcherHealth(lock, interval=1)
    yield producer, tmp_path
    producer.close()
    _release_singleton_lock(lock)


def test_scoped_expiring_receipt_and_invalidation(owner, monkeypatch):
    producer, root = owner
    board = root / "board.db"
    assert not health.available(root, board, "alice", "ready")
    producer.publish([(str(board.resolve()), ["alice"], ["ready"], health.time.monotonic())])
    assert health.available(root, board, "alice", "ready")
    assert not health.available(root, root / "other.db", "alice", "ready")
    assert not health.available(root, board, "bob", "ready")
    assert not health.available(root, board, "alice", "review")
    monkeypatch.setattr(health.time, "monotonic", lambda: 10**12)
    assert not health.available(root, board, "alice", "ready")


def test_cross_process_start_identity_lock_and_close(owner):
    producer, root = owner
    board = root / "board.db"
    producer.publish([(str(board), ["alice"], ["ready"], health.time.monotonic())])
    code = "from gateway.kanban_dispatcher_health import available; import sys; print(available(*sys.argv[1:]))"
    result = subprocess.run([sys.executable, "-c", code, str(root), str(board), "alice", "ready"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"
    path = root / ".dispatcher-health.json"
    record = json.loads(path.read_text())
    record["started_at"] = -1
    path.write_text(json.dumps(record))
    assert not health.available(root, board, "alice", "ready")
    producer.publish([(str(board), ["alice"], ["ready"], health.time.monotonic())])
    producer.close()
    assert not health.available(root, board, "alice", "ready")


def test_replacement_and_pause_are_fail_closed(owner):
    producer, root = owner
    board = root / "board.db"
    producer.publish([(str(board), ["alice"], ["ready"], health.time.monotonic())])
    producer.invalidate()
    assert not health.available(root, board, "alice", "ready")
    producer.publish([(str(board), ["alice"], ["ready"], health.time.monotonic())])
    (root / ".dispatcher.lock").unlink()
    (root / ".dispatcher.lock").touch()
    assert not health.available(root, board, "alice", "ready")


@pytest.mark.parametrize("ending", ["stop", "cancel", "pause", "error"])
def test_native_loop_publishes_only_actual_board_success(tmp_path, monkeypatch, ending):
    from gateway import kanban_watchers as watchers
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from types import SimpleNamespace

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda **kw: [SimpleNamespace(name="default")])
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "default")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(_KanbanDispatcher, "_board_slugs", lambda self: ["default", "broken"])
    real_connect = kbc.connect
    def connect(*args, **kwargs):
        if kwargs.get("board") == "broken":
            raise RuntimeError("test board unavailable")
        return real_connect(*args, **kwargs)
    monkeypatch.setattr(kbc, "connect", connect)
    board = kb.kanban_db_path("default")
    root = tmp_path / "kanban"
    lock, state = _acquire_singleton_lock(root / ".dispatcher.lock")
    assert state == "held"
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    runner._kanban_dispatcher_lock_handle = lock
    cfg = {"auto_decompose": False, "dispatch_interval_seconds": 1}
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {"kanban": cfg}, kb, cfg))
    async def no_sleep(delay):
        return None
    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)
    checks = []
    async def between(interval):
        checks.append(health.available(root, board, "default", "ready"))
        if len(checks) == 1:
            assert checks[-1]
            assert not health.available(root, kb.kanban_db_path("broken"), "default", "ready")
            if ending == "cancel":
                raise asyncio.CancelledError
            if ending == "pause":
                (tmp_path / "ESTOP").touch()
                assert not health.available(root, board, "default", "ready")
            elif ending == "error":
                monkeypatch.setattr(_KanbanDispatcher, "tick_once", lambda self: (_ for _ in ()).throw(RuntimeError("tick failed")))
            else:
                runner._running = False
        else:
            assert not checks[-1]
            runner._running = False
    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    if ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner._kanban_dispatcher_watcher())
    else:
        asyncio.run(runner._kanban_dispatcher_watcher())
    assert not health.available(root, board, "default", "ready")
    assert lock.closed


def test_one_shot_and_dry_run_never_publish(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_db_dispatch import dispatch_once
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    with kbc.connect(board="default") as conn:
        for dry_run in (True, False):
            dispatch_once(conn, board="default", dry_run=dry_run)
            assert not health.available(tmp_path / "kanban", kb.kanban_db_path("default"), "default", "ready")


def test_boot_identity_and_malformed_scope_fail_closed(owner):
    producer, root = owner
    board = root / "board.db"
    producer.publish([(str(board), ["alice"], ["ready"], health.time.monotonic())])
    path = root / ".dispatcher-health.json"
    record = json.loads(path.read_text())
    record["boot_id"] = "another-boot"
    path.write_text(json.dumps(record))
    assert not health.available(root, board, "alice", "ready")
    producer.publish([(str(board), ["alice"], ["ready"], health.time.monotonic())])
    record = json.loads(path.read_text())
    record["boards"] = [[str(board), "alice", "ready"]]
    path.write_text(json.dumps(record))
    assert not health.available(root, board, "alice", "ready")


def test_skipped_tick_does_not_produce_scope(tmp_path, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    monkeypatch.setattr(dispatcher, "_receipt_scope", lambda: ({"default"}, {"ready"}))
    monkeypatch.setattr(kbd, "dispatch_once", lambda *a, **kw: kbd.DispatchResult(skipped_locked=True))
    dispatcher.tick_once()
    assert dispatcher.successful_scopes == []


@pytest.mark.parametrize("mode", ["zero_budget", "critical_memory"])
def test_native_suppression_cannot_certify_scheduling(tmp_path, monkeypatch, mode):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    cfg = {"max_spawn": 0} if mode == "zero_budget" else {}
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "critical" if mode == "critical_memory" else "unknown")
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings(cfg, kb))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    monkeypatch.setattr(dispatcher, "_receipt_scope", lambda: ({"default"}, {"ready", "review"}))
    dispatcher.tick_once()
    assert dispatcher.successful_scopes == []


def test_delayed_publication_does_not_refresh_board_pass(owner, monkeypatch):
    producer, root = owner
    board = root / "board.db"
    clock = [100.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    scopes = [(str(board), ["alice"], ["ready"], health.time.monotonic())]
    clock[0] += 1000
    producer.publish(scopes)
    assert not health.available(root, board, "alice", "ready")


def test_slow_later_board_does_not_rejuvenate_earlier_scope(owner, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
    producer, root = owner
    monkeypatch.setattr(Path, "home", lambda: root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    kb.create_board("later")
    clock = [health.time.monotonic()]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    monkeypatch.setattr(dispatcher, "_receipt_scope", lambda: ({"alice"}, {"ready"}))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default", "later"])
    original = dispatcher.tick_once_for_board
    def tick(slug):
        if slug == "later":
            clock[0] += 1000
        return original(slug)
    monkeypatch.setattr(dispatcher, "tick_once_for_board", tick)
    dispatcher.tick_once()
    producer.publish(dispatcher.successful_scopes)
    assert not health.available(root, kb.kanban_db_path("default"), "alice", "ready")
    assert health.available(root, kb.kanban_db_path("later"), "alice", "ready")
