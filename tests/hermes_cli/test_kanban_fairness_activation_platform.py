"""Unsupported capture prerequisites must not interrupt normal startup."""
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_fairness_activation as activation
from hermes_cli import kanban_fairness_runtime as runtime


@pytest.mark.parametrize("missing", ["O_NONBLOCK", "O_NOFOLLOW", "O_DIRECTORY", "fcntl",
                                     "flock", "LOCK_EX", "LOCK_UN"])
@pytest.mark.parametrize("enabled", [True, False])
def test_boot_checks_platform_before_capture_io(tmp_path, monkeypatch, caplog, missing, enabled):
    cfg = {"kanban": {"fairness_capture": {"enabled": enabled, "capture_id": "fixture",
                                           "directory": str(tmp_path / "capture")}}}
    if missing == "fcntl":
        monkeypatch.setitem(sys.modules, "fcntl", None)
    elif missing in ("flock", "LOCK_EX", "LOCK_UN"):
        import fcntl
        proxy = SimpleNamespace(**{key: value for key, value in vars(fcntl).items() if key != missing})
        monkeypatch.setitem(sys.modules, "fcntl", proxy)
    else:
        # Do not remove flags from the global os module used by pytest/pathlib.
        proxy = SimpleNamespace(**{key: value for key, value in vars(os).items() if key != missing})
        monkeypatch.setattr(activation, "os", proxy)
    monkeypatch.setattr(activation, "_read", lambda *a: pytest.fail("capture IO before prerequisites"))
    monkeypatch.setattr(runtime, "activate", lambda *a, **kw: pytest.fail("unsupported session created"))
    with caplog.at_level(logging.WARNING, logger=activation.__name__):
        assert activation.boot(cfg, home=tmp_path) is None
    assert not (tmp_path / "capture").exists()
    messages = [record.message for record in caplog.records if record.name == activation.__name__]
    if enabled:
        assert any("fairness runtime startup incomplete" in message for message in messages)
    else:
        assert messages == []
