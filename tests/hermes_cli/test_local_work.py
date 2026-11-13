"""Foundation contracts, not gateway coverage/integration proof."""
import json
import os
import select
import subprocess
import sys
import threading

import pytest

from hermes_cli.local_work import ACTIVE_SCOPE, Registry


def test_seed_import_works_without_site_packages_or_work_start():
    code = """
import sys
import threading
before = threading.active_count()
import hermes_cli.local_work
assert threading.active_count() == before
assert not any(name == 'agent' or name.startswith('agent.') or
               name == 'gateway' or name.startswith('gateway.')
               for name in sys.modules)
assert not any('site-packages' in str(getattr(module, '__file__', ''))
               for module in sys.modules.values())
print('stdlib seed import: OK')
"""
    result = subprocess.run([sys.executable, "-S", "-c", code],
                            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stdlib seed import: OK"


def test_install_is_explicit_and_snapshot_is_detached(tmp_path, monkeypatch):
    import hermes_cli.local_work as module
    monkeypatch.setattr(module, "_registry", None)
    with pytest.raises(RuntimeError, match="not installed"):
        module.get_registry()
    registry = module.install(homes=[tmp_path])
    assert module.install(homes=[tmp_path]) is registry
    assert module.get_registry() is registry
    snapshot = registry.snapshot()
    with pytest.raises(TypeError):
        snapshot.per_kind["turn"] = 7
    token = registry.reserve("turn", home=tmp_path, source="test")
    assert snapshot.active == 0
    assert snapshot.per_kind == {}
    registry.mark_unknown("uncertain-component")
    registry.mark_unknown("uncertain-component")
    assert registry.snapshot().unknowns == ("uncertain-component",)
    assert registry.finish(token)


@pytest.mark.parametrize("kind,source", [("", "test"), (None, "test"),
                                         ("turn", ""), ("turn", None)])
def test_invalid_reservation_metadata_does_not_open_token(tmp_path, kind, source):
    registry = Registry(homes=[tmp_path])
    with pytest.raises(ValueError):
        registry.reserve(kind, home=tmp_path, source=source)
    assert registry.snapshot().active == 0
    assert registry.snapshot().epoch == 0


def test_epoch_counts_scope_and_sticky_unknown(tmp_path):
    home = str(tmp_path)
    registry = Registry(homes=[home])
    before = registry.snapshot()
    with registry.scope("turn", home=home, source="test") as owner:
        assert ACTIVE_SCOPE.get() is owner
        during = registry.snapshot()
        assert during.active == 1
        assert during.per_kind == {"turn": 1}
        assert during.epoch > before.epoch
    after = registry.snapshot()
    assert after.active == 0
    assert after.epoch > during.epoch
    assert ACTIVE_SCOPE.get() is None
    token = registry.reserve("thread", home=None, source="unbound")
    assert registry.snapshot().unknowns
    assert registry.finish(token)
    assert not registry.finish(token)
    unknowns = registry.snapshot().unknowns
    with registry.scope("turn", home=home, source="later"):
        pass
    assert registry.snapshot().unknowns == unknowns


def test_foreign_token_cannot_release_other_registry(tmp_path):
    home = str(tmp_path)
    first = Registry(homes=[home])
    second = Registry(homes=[home])
    token = first.reserve("turn", home=home, source="test")
    assert not second.finish(token)
    assert first.snapshot().active == 1
    assert second.snapshot().unknowns
    assert first.finish(token)


def test_token_matching_uses_identity_not_user_equality(tmp_path):
    home = str(tmp_path)
    registry = Registry(homes=[home])
    token = registry.reserve("turn", home=home, source="test")
    class Impostor:
        def __hash__(self):
            return hash(token)
        def __eq__(self, other):
            return True
    assert not registry.finish(Impostor())
    assert registry.snapshot().active == 1
    assert registry.finish(token)
    assert not registry.finish([])


def test_nested_scope_exception_restores_owner(tmp_path):
    home = str(tmp_path)
    registry = Registry(homes=[home])
    with registry.scope("outer", home=home, source="outer") as owner:
        with pytest.raises(ValueError):
            with registry.scope("inner", home=home, source="inner"):
                assert registry.snapshot().active == 2
                raise ValueError("target failed")
        assert ACTIVE_SCOPE.get() is owner
        assert registry.snapshot().active == 1
    assert registry.snapshot().active == 0
    assert registry.snapshot().unknowns == ()


@pytest.mark.platforms("posix")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_fork_invalidates_registry_even_when_parent_lock_is_held(tmp_path):
    home = str(tmp_path)
    registry = Registry(homes=[home])
    token = registry.reserve("turn", home=home, source="parent")
    locked, release = threading.Event(), threading.Event()
    def lock_holder():
        with registry._lock:
            locked.set()
            assert release.wait(10)
    thread = threading.Thread(target=lock_holder)
    thread.start()
    assert locked.wait(5)
    read_fd, write_fd = os.pipe()
    child = None
    try:
        child = os.fork()
        if child == 0:
            try:
                os.close(read_fd)
                snapshot = registry.snapshot()
                os.write(write_fd, json.dumps({"active": snapshot.active,
                         "unknowns": snapshot.unknowns,
                         "scope_clear": ACTIVE_SCOPE.get() is None}).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        assert select.select([read_fd], [], [], 5)[0], "fork child deadlocked"
        result = json.loads(os.read(read_fd, 4096))
        assert result == {"active": 0, "unknowns": ["fork-inherited-registry"],
                          "scope_clear": True}
    finally:
        release.set()
        thread.join(5)
        os.close(read_fd)
        if child:
            # If a regression deadlocks, terminate rather than hang pytest.
            waited, _ = os.waitpid(child, os.WNOHANG)
            if waited == 0:
                import signal
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
    assert registry.snapshot().active == 1
    assert registry.finish(token)
    assert registry.snapshot().unknowns == ()


def test_concurrent_short_workers_advance_epoch(tmp_path):
    home = str(tmp_path)
    registry = Registry(homes=[home])
    def work():
        for _ in range(100):
            token = registry.reserve("callback", home=home, source="worker")
            assert registry.finish(token)
    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    snapshot = registry.snapshot()
    assert snapshot.active == 0
    assert snapshot.epoch == 800
    assert snapshot.unknowns == ()
