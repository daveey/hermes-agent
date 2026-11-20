"""``session.create`` composer overrides and the model×provider coherence gate (#96817).

A composer, script or older client can pin a model the selected provider cannot serve
(``gpt-5.5`` on ``anthropic``); the session used to be minted fine and the FIRST turn died with
the provider's 404, leaving a dead chat. The gate is offline (curated catalogs only) and stays
permissive wherever Hermes cannot know better — see ``models_validate.static_model_provider_conflict``.
"""

from __future__ import annotations

import contextlib


def model_override_conflict(params: dict, build_scope) -> dict | None:
    """The conflict record for the create params' model override, or ``None`` when runnable /
    undecidable. Judged inside ``build_scope`` (the handler's ``_profile_build_scope(profile_home)``:
    the config and secrets the session's turns bind). First the managed subscription policy: the pin
    ``create_overrides`` stores, judged by the turn's own ``pin_policy``, so a refused pick never mints
    a chat whose every turn fails. Then catalog coherence; without an explicit ``provider`` the pair
    is judged against the provider the session would actually build with (profile config, then env)."""
    model = str(params.get("model") or "").strip()
    if not model:
        return None
    from hermes_cli.model_options_managed import managed_pin_refusal
    from hermes_cli.models_validate import static_model_provider_conflict
    from hermes_cli.runtime_provider import resolve_requested_provider

    provider = str(params.get("provider") or "").strip()
    # Derived where ``create_overrides`` derives it, so the judged pin is the one the session stores.
    pin = _prefer_subscription_route({"model": model, "provider": provider or None})
    with build_scope:
        refusal = managed_pin_refusal(pin["model"], pin["provider"])
        provider = provider or resolve_requested_provider()
    if refusal is not None:
        # Fits the Desktop toast, which swaps a message over 180 chars for its fallback
        # (``summarizeErrorMessage``); the allowed pins ride in ``data``, and the picker lists them.
        on = f" on {refusal.provider!r}" if refusal.provider else ""
        return {"message": f"{refusal.model!r}{on} can't run on this host: its managed subscription policy "
                           "refuses that pin. Pick a model the picker lists.",
                "model": refusal.model, "provider": refusal.provider, "allowed": refusal.allowed}
    return static_model_provider_conflict(model, provider)


def _prefer_subscription_route(override: dict) -> dict:
    """An unqualified model choice prefers the subscription; an explicit provider is honored.

    The picker labels the API row as billed. No extra, undeclared RPC flag is needed to choose it.
    """
    from hermes_cli.models_detect import preferred_subscription_model

    if (override.get("provider") or "").strip().lower() not in {"", "auto"}:
        return override
    if model := preferred_subscription_model(override["model"]):
        return {**override, "model": model, "provider": "openai-codex"}
    return override


def create_overrides(params: dict) -> tuple:
    """PER-SESSION (model, reasoning, service_tier) overrides from the composer — never a global config
    write. An explicit ``service_tier`` wins over legacy ``fast`` (an unknown word raises ``ValueError``);
    ``fast`` presence: omitted inherits, true pins priority, false pins normal (\"\")."""
    from utils import is_truthy_value

    model = str(params.get("model") or "").strip()
    model_override = {"model": model, "provider": str(params.get("provider") or "").strip() or None} if model else None
    if model_override:
        model_override = _prefer_subscription_route(model_override)
    reasoning_override = None
    if effort := str(params.get("reasoning_effort") or "").strip():
        with contextlib.suppress(Exception):
            from hermes_constants import parse_reasoning_effort
            reasoning_override = parse_reasoning_effort(effort)
    service_tier_override = None
    if params.get("service_tier") is not None:
        from agent.fast_mode import parse_exact_service_tier
        service_tier_override = parse_exact_service_tier(params["service_tier"])
    elif "fast" in params:
        service_tier_override = "priority" if is_truthy_value(params.get("fast")) else ""
    return model_override, reasoning_override, service_tier_override
