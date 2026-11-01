"""Bind a pinned Kanban model to a provider at pin time [daveey].

A card that pins a concrete model (``claude-opus-5-5``) with no provider used to late-bind the provider at
spawn: the dispatcher passed only ``-m <model>`` and the worker paired it with whatever ``model.provider``
the profile's config.yaml said at that moment. When that config flipped to ``provider: auto`` (with a
``class:deep`` default) for ~50 minutes, the auto ladder ended at OpenRouter, which rejected the bare
Anthropic id (HTTP 400), and the worker crashed with exit 78 — the same card had run fine on anthropic
twice before.

:func:`resolve_pin_provider` is the one resolver every pin path uses (``kanban_create``,
``hermes kanban create --model``, ``set-model``, the dashboard — all via ``kanban_db``), and the dispatcher
uses it for legacy rows. A concrete model without a provider gets, deterministically:

1. the assignee profile's configured ``model.provider`` when that provider serves the id (its static or
   cached live catalog lists it exactly, or it is the model vendor's own first-party provider) and holds
   credentials; a ``custom``/``custom:*`` profile provider is taken only for ids no known catalog/vendor
   claims (a private endpoint's own model name);
2. otherwise the single authenticated provider whose static catalog lists that exact id;
3. otherwise (no catalog lists it) the single authenticated first-party provider of the model's vendor.

No candidate, or more than one, raises :class:`PinProviderUnresolvedError` naming the providers to pass.
Class selectors (``class:deep``) are untouched: they keep provider auto by design.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_NO_PREFERENCE = frozenset({"", "auto"})


class PinProviderUnresolvedError(ValueError):
    code = "pin_provider_unresolved"

    def __init__(self, model: str, detail: str, providers: list[str]):
        self.model, self.providers = model, list(providers)
        hint = (f"pass provider= one of: {', '.join(self.providers)}" if self.providers
                else "pass provider=<provider that serves it>")
        super().__init__(
            f"{self.code}: model {model!r} was pinned without a provider and {detail}; {hint} "
            "(kanban_create provider=..., `hermes kanban create --provider ...`, `set-model ... --provider ...`)")


def _canon(provider: Optional[str]) -> str:
    pid = (provider or "").strip().lower()
    if not pid or pid.startswith("custom:") or pid in _NO_PREFERENCE:
        return pid
    try:
        from hermes_cli.models import normalize_provider
        return normalize_provider(pid) or pid
    except Exception:
        return pid


def _profile_scope(assignee: Optional[str], stack: ExitStack) -> Optional[Path]:
    """Enter the assignee profile's HERMES_HOME + secret scope (the worker's view); None = current home."""
    if not assignee:
        return None
    try:
        from hermes_cli.profiles import resolve_profile_env
        home = Path(resolve_profile_env(assignee))
    except Exception:
        return None  # non-profile lane: resolve against the current home
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    stack.callback(reset_hermes_home_override, set_hermes_home_override(str(home)))
    stack.callback(reset_secret_scope, set_secret_scope(build_profile_secret_scope(home)))
    return home


def _configured_provider(home: Optional[Path]) -> str:
    try:
        from hermes_cli.config_effective import load_profile_config_effective, load_user_config_effective
        config = (load_profile_config_effective(home / "config.yaml", fail_closed=True)
                  if home else load_user_config_effective())
    except Exception as exc:
        logger.debug("kanban pin provider: config unreadable (%s)", exc)
        return ""
    block = (config or {}).get("model")
    return _canon(block.get("provider")) if isinstance(block, dict) else ""


def _static_candidates(model: str) -> list[str]:
    from hermes_cli.models import _static_catalog_matches
    out: list[str] = []
    for pid, name in _static_catalog_matches(model, ""):
        pid = _canon(pid)
        if name.lower() == model.lower() and pid not in out:
            out.append(pid)
    return out


def _vendor_candidates(model: str) -> list[str]:
    from hermes_cli.models import _PROVIDER_MODELS
    from hermes_cli.models_detect import current_provider_owns_vendor
    out: list[str] = []
    for pid in _PROVIDER_MODELS:
        canon = _canon(pid)
        if canon not in out and current_provider_owns_vendor(model, pid):
            out.append(canon)
    return out


def _serves(provider: str, model: str) -> bool:
    from hermes_cli.models import _model_in_provider_catalog, _provider_keys
    from hermes_cli.models_detect import current_provider_catalog_match, current_provider_owns_vendor
    if _model_in_provider_catalog(model.lower(), _provider_keys(provider)):
        return True
    if current_provider_owns_vendor(model, provider):
        return True
    try:
        return current_provider_catalog_match(model, provider) == model
    except Exception:
        return False


def resolve_pin_provider(
    model: Optional[str], provider: Optional[str], *, assignee: Optional[str] = None,
    has_credentials: Optional[Callable[[str], bool]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """``(model, provider)`` with the provider bound for a concrete provider-less pin; see module doc.
    Raises :class:`PinProviderUnresolvedError` when no single authenticated provider qualifies."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if not model or provider:
        return model, provider
    from agent.model_routing import is_class_selector
    if is_class_selector(model):
        return model, provider
    if has_credentials is None:
        from hermes_cli.models_detect import provider_has_credentials as has_credentials
    with ExitStack() as stack:
        home = _profile_scope(assignee, stack)
        preferred = _configured_provider(home)
        static = _static_candidates(model)
        vendor = [] if static else _vendor_candidates(model)
        known = static or vendor
        if preferred.startswith("custom") and not known and has_credentials(preferred):
            return model, preferred  # a private endpoint's own model name: the profile's endpoint serves it
        if (preferred and preferred not in _NO_PREFERENCE and not preferred.startswith("custom")
                and (preferred in known or (not known and _serves(preferred, model)))
                and has_credentials(preferred)):
            return model, preferred
        authed = [p for p in known if has_credentials(p)]
    if len(authed) == 1:
        return model, authed[0]
    if len(authed) > 1:
        raise PinProviderUnresolvedError(model, f"several authenticated providers serve it ({', '.join(authed)})",
                                         authed)
    if known:
        raise PinProviderUnresolvedError(
            model, f"no provider that serves it has credentials here ({', '.join(known)} known)", known)
    raise PinProviderUnresolvedError(model, "no known provider catalog lists that id", [])


def require_remote_pin_provider(model: Optional[str], provider: Optional[str]) -> None:
    """Refuse a concrete provider-less pin on a card that runs on another machine. [daveey]

    :func:`resolve_pin_provider` binds against THIS machine's profile and credentials, which say nothing about
    the landing machine's, and the landing dispatcher's :func:`resolve_legacy_pin` only logs when it cannot
    bind, so the worker would late-bind to its config at spawn (the exit-78 failure above). The creator
    passes the provider instead; class selectors keep provider auto."""
    model = (model or "").strip()
    if not model or (provider or "").strip():
        return
    from agent.model_routing import is_class_selector
    if is_class_selector(model):
        return
    known = _static_candidates(model) or _vendor_candidates(model)
    raise PinProviderUnresolvedError(
        model, "the card runs on another machine, whose credentials this machine cannot check", known)


def resolve_legacy_pin(task: Any) -> tuple[Optional[str], Optional[str]]:
    """Dispatcher path for rows pinned before the pin-time resolver: ``(provider, error)``. Never raises."""
    model = (getattr(task, "model_override", None) or "").strip()
    if not model or (getattr(task, "provider_override", None) or "").strip() or model.startswith("class:"):
        return None, None
    try:
        return resolve_pin_provider(model, None, assignee=getattr(task, "assignee", None))[1], None
    except PinProviderUnresolvedError as exc:
        return None, str(exc)
    except Exception as exc:  # never break dispatch over provider lookup
        logger.debug("kanban pin provider: legacy resolve failed for %s (%s)", getattr(task, "id", "?"), exc)
        return None, f"error: {exc}"
