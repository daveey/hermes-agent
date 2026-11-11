"""Darwin metrics must not fork an otherwise idle gateway."""
import subprocess
import sys
from types import SimpleNamespace

from hermes_cli import machine_metrics as mm


def test_darwin_reader_is_fresh_without_subprocess(monkeypatch):
    values = iter([75.0, 75.0, 62.5])
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "hermes_cli.machine_memory_darwin", SimpleNamespace(read_used_pct=lambda: next(values)))

    def forbidden(*a, **kw):
        raise AssertionError("idle memory metric spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert [mm._mem_used_pct() for _ in range(3)] == [75.0, 75.0, 62.5]


def test_darwin_reader_failure_omits_metric_without_spawn(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def unavailable():
        raise OSError("host statistics unavailable")

    monkeypatch.setitem(sys.modules, "hermes_cli.machine_memory_darwin", SimpleNamespace(read_used_pct=unavailable))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("spawn")))
    assert mm._mem_used_pct() is None
