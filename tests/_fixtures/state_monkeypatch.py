"""``monkeypatch`` that records Starlette ``State`` attributes correctly on every pytest version.

Imported into ``tests/conftest.py`` so pytest registers the override there (see live_system_guard.py
for why ``pytest_plugins`` is not used).

pytest 9.2's ``MonkeyPatch.setattr`` reads the value it will restore from the target's instance
``__dict__``. Starlette's ``State`` (``app.state``, ``request.state``) keeps its attributes in
``self._state`` behind ``__getattr__``/``__setattr__``, so every setattr on it records "no old value":

* setting the same State attribute twice (a re-set, or a helper called twice) deletes it twice at
  undo, and the second delete raises ``KeyError`` at teardown;
* an attribute the app already had (set by ``create_app``) is deleted at undo instead of restored,
  so later tests on a module-level app see it missing.

pytest 9.0 read the old value with ``getattr`` and did neither. Routing ``setattr(<State>, name, v)``
to ``setitem(state._state, name, v)`` (and ``delattr`` to ``delitem``) records each old value from
the dict that really holds it, which is correct on both versions; every other target goes to
pytest unchanged.
"""
from __future__ import annotations

import sys
from typing import Any

import pytest

_UNSET: Any = object()
_STARLETTE_DATASTRUCTURES = "starlette.datastructures"


def _is_starlette_state(target: object) -> bool:
    # Never import starlette here: if it is not loaded yet, ``target`` cannot be one of its States.
    mod = sys.modules.get(_STARLETTE_DATASTRUCTURES)
    state_cls = getattr(mod, "State", None) if mod is not None else None
    return state_cls is not None and isinstance(target, state_cls)


def _state_store(target: object, name: object) -> dict[str, Any] | None:
    """The ``_state`` dict holding ``name`` when ``target`` is a State and ``name`` lives there."""
    if not (isinstance(name, str) and _is_starlette_state(target)):
        return None
    if name in vars(target) or hasattr(type(target), name):  # ``_state`` itself, methods, dunders
        return None
    return vars(target)["_state"]


def state_aware(mpatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Make ``mpatch.setattr``/``delattr`` on a Starlette ``State`` patch its ``_state`` dict.

    Both go through the ``setitem``/``delitem`` stack: ``undo()`` replays the setattr stack before
    the setitem stack, so routing only ``setattr`` would replay a ``delattr``-then-``setattr`` of one
    attribute in the wrong order. Dotted-string targets and ``MonkeyPatch.context()`` instances keep
    pytest's behaviour (no in-tree use touches a State that way).

    Wraps the instance methods rather than subclassing: ``pytest.MonkeyPatch`` is ``@final``, and
    the fixture must stay a real ``pytest.MonkeyPatch`` for code that checks or annotates it.
    Returns ``mpatch``.
    """
    native_setattr = mpatch.setattr
    native_delattr = mpatch.delattr

    def setattr(target: Any, name: Any = _UNSET, value: Any = _UNSET, raising: bool = True) -> None:
        __tracebackhide__ = True
        store = _state_store(target, name) if value is not _UNSET else None
        if store is None:
            native_setattr(target, *[a for a in (name, value) if a is not _UNSET], raising=raising)
            return
        if raising and name not in store:
            raise AttributeError(f"{target!r} has no attribute {name!r}")
        mpatch.setitem(store, name, value)

    def delattr(target: Any, name: Any = _UNSET, raising: bool = True) -> None:
        __tracebackhide__ = True
        store = _state_store(target, name)
        if store is None:
            native_delattr(target, *[a for a in (name,) if a is not _UNSET], raising=raising)
            return
        if name not in store:
            if raising:
                raise AttributeError(name)
            return
        mpatch.delitem(store, name)

    mpatch.setattr = setattr  # type: ignore[method-assign]
    mpatch.delattr = delattr  # type: ignore[method-assign]
    return mpatch


@pytest.fixture(name="monkeypatch")
def _state_aware_monkeypatch():
    """Same contract as pytest's built-in ``monkeypatch``, with State-aware ``setattr``/``delattr``.

    Registered under a private function name: a conftest global named ``monkeypatch`` would be
    shadowed by every fixture that takes ``monkeypatch`` as a parameter (ruff F811).
    """
    mpatch = state_aware(pytest.MonkeyPatch())
    yield mpatch
    mpatch.undo()
