"""The tui_gateway.server reset fixture must tolerate a module another thread is still importing."""
import importlib.machinery
import sys
import types

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
