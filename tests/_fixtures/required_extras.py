"""Strict mode for optional-extra ``pytest.importorskip`` guards.

Test files guard optional SDKs (``keyring``, ``aiohttp``, ``acp``...) with a module-level
``pytest.importorskip`` so a minimal venv skips them instead of failing collection. In a CI
lane that installs those extras, the same skip would hide a dropped extra behind a green run.

``HERMES_TEST_REQUIRE_EXTRAS`` (comma-separated top-level module names) turns the skip into a
collection error for the listed modules: ``tests/conftest.py`` calls :func:`install` at import
time, before any test module runs its guard. Unlisted modules keep importorskip's skip, and an
unset variable leaves ``pytest.importorskip`` untouched.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any

import pytest

ENV_VAR = "HERMES_TEST_REQUIRE_EXTRAS"


def required_modules(value: str | None) -> frozenset[str]:
    """Top-level module names listed in *value* (the env var's text)."""
    if not value:
        return frozenset()
    return frozenset(part.strip().split(".", 1)[0] for part in value.split(",") if part.strip())


def strict_importorskip(original: Callable[..., Any], required: frozenset[str]) -> Callable[..., Any]:
    """Wrap *original* so a module whose top-level name is in *required* must import."""

    def importorskip(modname: str, *args: Any, **kwargs: Any) -> Any:
        if modname.split(".", 1)[0] in required:
            try:
                importlib.import_module(modname)
            except ImportError as exc:
                raise ImportError(
                    f"{modname!r} is required in this run ({ENV_VAR}={','.join(sorted(required))}) "
                    f"but cannot be imported: {exc}. Install its extra instead of skipping."
                ) from exc
        return original(modname, *args, **kwargs)

    importorskip.__wrapped__ = original  # type: ignore[attr-defined]
    importorskip.__doc__ = original.__doc__
    return importorskip


def install(environ: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    """Replace ``pytest.importorskip`` when *environ* lists required extras; True if it did."""
    required = required_modules(environ.get(ENV_VAR))
    if not required:
        return False
    current = pytest.importorskip
    pytest.importorskip = strict_importorskip(getattr(current, "__wrapped__", current), required)
    return True
