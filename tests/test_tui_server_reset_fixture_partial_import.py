"""The tui_gateway.server reset fixture must tolerate a module another thread is still importing."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import importlib.machinery
import sys
import types

import pytest
from unittest.mock import Mock

from tests import conftest as root_conftest


def _fixture_body():
    fx = root_conftest._reset_tui_gateway_server_state
    return getattr(fx, "__wrapped__", None) or fx.__pytest_wrapped__.obj


def test_partially_initialized_server_module_is_skipped(monkeypatch):
    mod = types.ModuleType("tui_gateway.server")  # no _methods/_sessions yet, like mid-import
    spec = importlib.machinery.ModuleSpec("tui_gateway.server", None)
    spec._initializing = True
    mod.__spec__ = spec
    monkeypatch.setitem(sys.modules, "tui_gateway.server", mod)
    assert root_conftest._tui_server_importing(mod) is True
    gen = _fixture_body()()
    next(gen)  # setup must not touch the missing globals
    try:
        next(gen)  # teardown must not either
    except StopIteration:
        pass
    # Left "initializing" so the outer autouse fixture's own teardown is exercised too.


def test_fully_initialized_module_is_not_skipped():
    mod = types.ModuleType("x")
    mod.__spec__ = importlib.machinery.ModuleSpec("x", None)
    assert root_conftest._tui_server_importing(mod) is False


@pytest.mark.parametrize("initializing", [True, False])
def test_notification_park_cleanup_respects_import_state(monkeypatch, initializing):
    server = types.SimpleNamespace(
        _methods={}, _sessions={}, _cfg_cache=None, _cfg_sig=None, _cfg_path=None,
        _db=None, _db_error=None, _real_stdout=None,
    )
    park = types.ModuleType("tui_gateway.notification_park")
    park.__spec__ = importlib.machinery.ModuleSpec(park.__name__, None)
    park.__spec__._initializing = initializing
    clear = Mock()
    if not initializing:
        park.clear = clear
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    monkeypatch.setitem(sys.modules, park.__name__, park)
    gen = _fixture_body()()
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert clear.call_count == (0 if initializing else 1)
