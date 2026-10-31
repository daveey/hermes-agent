"""``OPTIONAL_ENV_VARS``' import-time additions, run on first use. [daveey]

``hermes_cli.config`` adds every api-key provider's env vars and every platform plugin
manifest's entries to ``OPTIONAL_ENV_VARS``, then derives ``CORE_DECLARED_ENV_NAMES`` (between
the two stages) and ``BUNDLED_PLATFORM_SECRET_ENVS``. Doing that at import discovered every
provider plugin and read every platform manifest in each process importing the config module,
which a cold background helper (the kanban admission check) paid without reading the dict.

:class:`EnvVarRegistry` runs the stages on the first read or write of the dict and
:func:`value` on the first read of a derived name: once, in the eager order, under a re-entrant
lock (other threads wait; a nested touch from inside a stage sees the partial dict, as a read
during the eager import did). Leaf module: imports nothing from ``hermes_cli.config``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_stages: Optional[Callable[[], Iterator[tuple[str, Any]]]] = None
_home: Optional[str] = None
_values: dict[str, Any] = {}
_state = 0  # 0 pending, 1 running (the running thread holds the lock), 2 done


def install(stages: Callable[[], Iterator[tuple[str, Any]]],
            ensure_home: Optional[Callable[[], None]] = None) -> None:
    """Register the additions (a generator publishing each derived ``(name, value)`` as soon as
    it exists). Called by ``hermes_cli.config`` at import, so a reload re-runs them.

    ``ensure_home`` runs now: the eager provider discovery called ``load_config()`` at import,
    so importing the config module initialized and secured the home once (ensure_hermes_home's
    per-process memo). A later mode change (an operator's chmod, a read-only mount) must not be
    "repaired" by a first ensure moved to the first config read. Errors are logged and dropped,
    as the eager path swallowed them inside ``_inject_profile_env_vars``.

    The Hermes home bound now is the one the stages read later: the eager import read the
    provider and platform plugins of the home bound at import, and a first touch from a turn
    scoped to another profile must not swap in that profile's plugins process-wide."""
    global _stages, _state, _home
    from hermes_constants import get_hermes_home

    with _lock:
        _stages, _state, _home = stages, 0, str(get_hermes_home())
        _values.clear()
    if ensure_home is not None:
        try:
            ensure_home()
        except Exception:  # health: allow BLE001 -- the eager import swallowed this too
            logger.debug("Hermes home initialization at config import failed", exc_info=True)


def ensure() -> None:
    """Run the additions once. The lock is held across the stages' imports (as the eager import
    held the config module's import lock); a stage that re-enters sees the partial dict."""
    global _state
    if _state == 2 or _stages is None:
        return
    with _lock:
        if _state or _stages is None:
            return
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        _state = 1
        token = set_hermes_home_override(_home)
        try:
            for name, val in _stages():
                _values[name] = val
            _state = 2
        except BaseException:
            # The eager import failed here; leave the work pending so the next touch retries
            # rather than serving a half-built registry.
            _values.clear()
            _state = 0
            raise
        finally:
            reset_hermes_home_override(token)


def value(name: str) -> Any:
    ensure()
    return _values[name]


def done() -> bool:
    return _state == 2


class EnvVarRegistry(dict):
    """A dict whose every read/write method first runs the registered additions."""

    __slots__ = ()


def _hooked(name: str):
    base = getattr(dict, name)

    def method(self, *args, **kwargs):
        ensure()
        return base(self, *args, **kwargs)
    method.__name__ = method.__qualname__ = name
    method.__doc__ = base.__doc__
    return method


for _name in ("__getitem__", "__setitem__", "__delitem__", "__contains__", "__iter__", "__len__",
              "__reversed__", "__repr__", "__eq__", "__ne__", "__or__", "__ror__", "__ior__",
              "__reduce__", "__reduce_ex__", "__sizeof__", "get", "keys", "values", "items", "copy",
              "setdefault", "pop", "popitem", "update", "clear"):
    setattr(EnvVarRegistry, _name, _hooked(_name))
del _name


def install_config(config) -> None:
    """Keep platform manifests lazy while the bundled provider catalog reads no config.

    The facade owns monkeypatch seams; late reads preserve them and reload behavior.
    Home initialization still occurs at import unless the worker supplied a snapshot.
    """
    from hermes_cli import config_provider_env as providers

    def stages():
        config._inject_profile_env_vars()
        core = frozenset(dict.keys(config.OPTIONAL_ENV_VARS))
        providers._PROVIDER_NAMES.update(core - providers._STATIC_NAMES)
        yield "CORE_DECLARED_ENV_NAMES", core
        yield "BUNDLED_PLATFORM_SECRET_ENVS", config._inject_platform_plugin_env_vars()

    def ensure_home():
        if config._worker_config_snapshot() is None:
            config.ensure_hermes_home()

    install(stages, ensure_home=ensure_home)


def _inject_profile_env_vars() -> None:
    """Expose env_vars of every ``auth_type="api_key"`` provider in providers/ via OPTIONAL_ENV_VARS
    without editing this file."""
    from hermes_cli.config_defaults import OPTIONAL_ENV_VARS
    try:
        from providers import bundled_profiles
        for _pp in bundled_profiles():
            if _pp.auth_type != "api_key":
                continue
            for _var in _pp.env_vars:
                if _var in OPTIONAL_ENV_VARS:
                    continue
                _is_key = not _var.endswith(("_BASE_URL", "_URL"))
                _label = _pp.display_name or _pp.name
                OPTIONAL_ENV_VARS[_var] = {
                    "description": f"{_label} {'API key' if _is_key else 'base URL override'}",
                    "prompt": f"{_label} {'API key' if _is_key else 'base URL (leave empty for default)'}",
                    "url": _pp.signup_url or None,
                    "password": _is_key,
                    "category": "provider",
                    "advanced": True}
    except Exception:  # health: allow BLE001 -- moved verbatim from config.py: a broken provider plugin must not break config
        pass
