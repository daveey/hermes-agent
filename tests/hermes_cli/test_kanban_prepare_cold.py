"""Cold preparation must not activate sync accounting; warm steps retain marks."""
import sys
import threading
from types import ModuleType

import pytest

from hermes_cli import kanban_generation as generation
from hermes_cli import kanban_lockguard as lockguard

MODULE = "hermes_cli.kanban_sync_cpu"


@pytest.mark.parametrize("module_state", ["absent", "partial", "loaded"])
@pytest.mark.parametrize("failure", [None, "guard", "remember"])
def test_prepare_order_and_exceptions(monkeypatch, module_state, failure):
    calls = []
    error = RuntimeError("preparation refused")

    class Connection:
        def execute(self, sql):
            calls.append(sql)

    conn = Connection()

    def guard(actual, *, strict):
        assert actual is conn and strict is True
        calls.append("guard")
        if failure == "guard":
            raise error

    def remember(actual, *, strict):
        assert actual is conn and strict is True
        calls.append("remember")
        if failure == "remember":
            raise error

    def mark(part, fn, *args, **kwargs):
        calls.append(part)
        return fn(*args, **kwargs)

    monkeypatch.setattr(lockguard, "guard", guard)
    monkeypatch.setattr(generation, "remember", remember)
    module = ModuleType(MODULE)
    if module_state == "loaded":
        module.mark = mark
    if module_state == "absent":
        monkeypatch.delitem(sys.modules, MODULE, raising=False)
    else:
        monkeypatch.setitem(sys.modules, MODULE, module)
    if failure:
        with pytest.raises(RuntimeError) as caught:
            generation.prepare_connection(conn, .123)
        assert caught.value is error
    else:
        generation.prepare_connection(conn, .123)
    expected = ["PRAGMA busy_timeout=0"]
    if module_state == "loaded":
        expected.append("prepare.guard")
    expected.append("guard")
    if failure != "guard":
        if module_state == "loaded":
            expected.append("prepare.remember")
        expected.append("remember")
    if failure is None:
        expected.append("PRAGMA busy_timeout=123")
    assert calls == expected
    if module_state == "absent":
        assert MODULE not in sys.modules


@pytest.mark.parametrize("failure", [None, "guard", "remember"])
def test_prepare_marks_after_late_sync_activation(monkeypatch, failure):
    from hermes_cli import kanban_sync_cpu as cpu

    records = []
    error = RuntimeError("strict preparation failed")
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(cpu.loop_accounting, "scopes_enabled", lambda: False)
    monkeypatch.setattr(cpu.loop_accounting, "record", lambda name, **kw: records.append((name, kw)))

    class Connection:
        def execute(self, sql):
            pass

    def guard(conn, *, strict):
        assert strict is True
        if failure == "guard":
            raise error

    def remember(conn, *, strict):
        assert strict is True
        if failure == "remember":
            raise error

    monkeypatch.setattr(lockguard, "guard", guard)
    monkeypatch.setattr(generation, "remember", remember)
    conn = Connection()
    with monkeypatch.context() as cold:
        cold.delitem(sys.modules, MODULE)
        # Preparation before accounting activation still guards/registers.
        if failure:
            with pytest.raises(RuntimeError):
                generation.prepare_connection(conn, .1)
        else:
            generation.prepare_connection(conn, .1)
        assert MODULE not in sys.modules
    # Now the real initialized module is visible; don't cache the cold choice.
    if not failure:
        generation.prepare_connection(conn, .1)
    assert records == []  # loaded but outside a step is still a plain call

    def prepare():
        try:
            generation.prepare_connection(conn, .1)
        finally:
            assert records == []  # no telemetry publication under the lifecycle lock

    if failure:
        with pytest.raises(RuntimeError) as caught:
            cpu.step(prepare)
        assert caught.value is error
    else:
        cpu.step(prepare)
    names = {name: kw for name, kw in records}
    expected = {"sync.detail/prepare.prepare.guard"}
    if failure != "guard":
        expected.add("sync.detail/prepare.prepare.remember")
    assert set(names) == expected
    assert all(kw["rows"] == 1 and kw["outer"] is False for kw in names.values())
