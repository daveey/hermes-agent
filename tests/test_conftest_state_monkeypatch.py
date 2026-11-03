"""The ``monkeypatch`` fixture restores Starlette ``State`` attributes on every pytest version.

pytest 9.2 records State attributes as "no old value" (they live in ``State._state``, not the
instance ``__dict__``), so a double setattr raised ``KeyError`` at teardown and a pre-existing
attribute was deleted instead of restored. tests/_fixtures/state_monkeypatch.py routes State
targets through ``setitem``; these tests call ``undo()`` explicitly and pass on pytest 9.0 and 9.2.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import types

import pytest

from tests._fixtures.state_monkeypatch import state_aware

State = pytest.importorskip("starlette.datastructures").State


def test_fixture_is_still_a_pytest_monkeypatch(monkeypatch):
    assert isinstance(monkeypatch, pytest.MonkeyPatch)


def test_double_setattr_of_new_state_attribute_undoes_cleanly(monkeypatch):
    state = State()
    monkeypatch.setattr(state, "session_authority", "first", raising=False)
    monkeypatch.setattr(state, "session_authority", "second", raising=False)
    assert state.session_authority == "second"

    monkeypatch.undo()

    assert "session_authority" not in state._state


def test_double_setattr_of_existing_state_attribute_restores_original(monkeypatch):
    state = State({"ssh_isolated_clients": "original"})
    monkeypatch.setattr(state, "ssh_isolated_clients", "a")
    monkeypatch.setattr(state, "ssh_isolated_clients", "b")
    assert state.ssh_isolated_clients == "b"

    monkeypatch.undo()

    assert state.ssh_isolated_clients == "original"


def test_app_state_attribute_survives_patching(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    app = fastapi.FastAPI()
    app.state.auth_required = True
    monkeypatch.setattr(app.state, "auth_required", False)

    monkeypatch.undo()

    assert app.state.auth_required is True


def test_delattr_then_setattr_of_existing_state_attribute_restores_original(monkeypatch):
    # The "drop it if present, then set it" pattern (test_ui_preferences.py and others): undo
    # must replay both steps in reverse order across pytest's setattr and setitem stacks.
    state = State({"ui_surface": "original"})
    monkeypatch.delattr(state, "ui_surface")
    monkeypatch.setattr(state, "ui_surface", "patched", raising=False)

    monkeypatch.undo()

    assert state.ui_surface == "original"


def test_setattr_then_delattr_of_existing_state_attribute_restores_original(monkeypatch):
    state = State({"ui_surface": "original"})
    monkeypatch.setattr(state, "ui_surface", "patched")
    monkeypatch.delattr(state, "ui_surface")
    assert "ui_surface" not in state._state

    monkeypatch.undo()

    assert state.ui_surface == "original"


def test_delattr_of_missing_state_attribute_raises_unless_raising_false(monkeypatch):
    state = State()
    with pytest.raises(AttributeError):
        monkeypatch.delattr(state, "absent")
    monkeypatch.delattr(state, "absent", raising=False)
    monkeypatch.undo()
    assert "absent" not in state._state


def test_missing_state_attribute_raises_unless_raising_false():
    mpatch = state_aware(pytest.MonkeyPatch())
    state = State()
    with pytest.raises(AttributeError):
        mpatch.setattr(state, "absent", 1)
    mpatch.setattr(state, "absent", 1, raising=False)
    assert state.absent == 1
    mpatch.undo()
    assert "absent" not in state._state


def test_non_state_targets_keep_native_behaviour(monkeypatch):
    target = types.SimpleNamespace(value="original")
    monkeypatch.setattr(target, "value", "a")
    monkeypatch.setattr(target, "value", "b")
    monkeypatch.setattr("os.sep", "!")
    import os

    assert (target.value, os.sep) == ("b", "!")
    monkeypatch.undo()
    assert target.value == "original"
    assert os.sep != "!"
