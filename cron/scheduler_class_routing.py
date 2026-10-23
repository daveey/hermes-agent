"""Cron class requests defer selection until the per-fire session exists."""
from agent.model_routing import ModelRoutingError, is_class_selector


def deferred_runtime(model, provider, job):
    if not is_class_selector(model):
        return None
    if provider not in (None, "", "auto") or job.get("base_url"):
        raise ModelRoutingError("Cron classes require provider auto and no endpoint override")
    return {"provider": "auto", "requested_provider": "auto"}


def preflight_is_class(job, config):
    # Match _load_cron_job_config precedence, without a credential probe or a
    # durable allocation. Availability is checked at actual job construction.
    from cron.env_settings import cron_env_setting
    cron = config.get("cron") or {}
    main = config.get("model") or {}
    main_model = main if isinstance(main, str) else (
        main.get("default") or main.get("model") or main.get("name") if isinstance(main, dict) else "")
    cron_model = cron.get("model") if isinstance(cron, dict) else None
    model = job.get("model") or cron_model or main_model or cron_env_setting("HERMES_MODEL") or ""
    return is_class_selector(model)
