"""``session.create`` composer overrides and the model×provider coherence gate (#96817).

A composer, script or older client can pin a model the selected provider cannot serve
(``gpt-5.5`` on ``anthropic``); the session used to be minted fine and the FIRST turn died with
the provider's 404, leaving a dead chat. The gate is offline (curated catalogs only) and stays
permissive wherever Hermes cannot know better — see ``models_validate.static_model_provider_conflict``.
"""

from __future__ import annotations

import contextlib


def model_override_conflict(params: dict, build_scope) -> dict | None:
    """The conflict record for the create params' model override, or ``None`` when coherent /
    undecidable. Without an explicit ``provider`` the pair is judged against the provider the
    session would actually build with (profile config, then env) inside ``build_scope`` — the
    handler's ``_profile_build_scope(profile_home)`` context manager."""
    model = str(params.get("model") or "").strip()
    if not model:
        return None
    from hermes_cli.models_validate import static_model_provider_conflict
    from hermes_cli.runtime_provider import resolve_requested_provider

    provider = str(params.get("provider") or "").strip()
    if not provider:
        with build_scope:
            provider = resolve_requested_provider()
    return static_model_provider_conflict(model, provider)


def _prefer_subscription_route(override: dict, params: dict) -> dict:
    """Re-route a metered OpenAI API pick to the ChatGPT/Codex subscription when it serves the same
    model, unless the client says the metered route was a deliberate choice (``allow_metered``).
    A picker that only offered the metered row once billed ~$330 for chats meant for the subscription."""
    from utils import is_truthy_value

    provider = (override.get("provider") or "").strip().lower()
    if provider not in {"openai", "openai-api"} or is_truthy_value(params.get("allow_metered")):
        return override
    try:
        from hermes_cli.models import cached_provider_model_ids

        served = {m.lower() for m in cached_provider_model_ids("openai-codex", non_blocking=True)}
    except Exception:
        return override
    if override["model"].lower() not in served:
        return override
    import logging
    logging.getLogger(__name__).warning(
        "session.create: %s requested on metered provider %s; subscription provider openai-codex serves it — "
        "routing there (pass allow_metered to force the billed API)", override["model"], provider)
    return {**override, "provider": "openai-codex"}


def create_overrides(params: dict) -> tuple:
    """PER-SESSION (model, reasoning, service_tier) overrides from the composer — never a global config
    write. An explicit ``service_tier`` wins over legacy ``fast`` (an unknown word raises ``ValueError``);
    ``fast`` presence: omitted inherits, true pins priority, false pins normal (\"\")."""
    from utils import is_truthy_value

    model = str(params.get("model") or "").strip()
    model_override = {"model": model, "provider": str(params.get("provider") or "").strip() or None} if model else None
    if model_override:
        model_override = _prefer_subscription_route(model_override, params)
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
