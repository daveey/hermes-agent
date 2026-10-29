"""Stored cron model pins vs the managed routing registry. [daveey]

Under ``model_routing.enforce_subscription_only`` every concrete model goes through
``pin_policy``, which needs an exact registry match. A job pinned before the registry changed
then dies at every woken run, before the model, with "Managed subscription policy rejects
unregistered or ambiguous concrete pin". This module runs that same check where it can be
acted on: the scheduler preflight (blocked_config, alert once), ``hermes cron create/edit``
(refuse the pin) and ``hermes cron doctor`` (list it). Kanban's sibling is
``hermes_cli.kanban_model_assignment._check_managed_pin``.
"""
from typing import Optional

CODE = "model_pin_not_in_routing_registry"


def effective_model(job: dict, cfg: dict) -> tuple[str, str]:
    """``(model, source)`` with run_job's precedence: job pin > cron.model > main model.

    ``source`` is ``"job"``, ``"cron.model"``, ``"model.default"`` or ``""`` (nothing set; HERMES_MODEL is
    process env, not a stored pin, so it is left to the run path).
    """
    if str(job.get("model") or "").strip():
        return str(job["model"]).strip(), "job"
    cron = cfg.get("cron")
    cron = cron if isinstance(cron, dict) else {}
    if str(cron.get("model") or "").strip():
        return str(cron["model"]).strip(), "cron.model"
    main = cfg.get("model")
    main = main if isinstance(main, str) else (
        (main.get("default") or main.get("model") or main.get("name")) if isinstance(main, dict) else "")
    return (str(main).strip(), "model.default") if str(main or "").strip() else ("", "")


def rejected_pin(model: Optional[str], provider: Optional[str], cfg: Optional[dict]) -> Optional[list]:
    """The registry's allowed pins when ``model`` would be refused; None when it passes or no policy."""
    from agent.model_routing import is_class_selector
    from agent.model_routing_managed import ManagedPinRejectedError, managed_config, pin_policy

    model = (model or "").strip()
    if not model or is_class_selector(model) or cfg is None:
        return None
    managed = managed_config(cfg)
    if managed is None:
        return None
    try:
        pin_policy(model, (provider or "").strip() or None, managed)
    except ManagedPinRejectedError as exc:
        return exc.allowed
    return None


def rejection_message(model: str, provider: Optional[str], allowed: list, *, job_id: Optional[str] = None,
                      source: str = "job") -> str:
    on = f" on provider {provider!r}" if provider else ""
    where = {"job": "this job's model pin", "cron.model": "config cron.model (the cron fleet default)",
             "model.default": "config model.default"}.get(source, "the model")
    fix = (f"`hermes cron edit {job_id} --model <one of those>`" if source == "job" and job_id
           else f"set {source} to one of those" if source != "job" else "pin one of those")
    return (f"{CODE}: {where} {model!r}{on} is not in this host's model routing registry "
            f"(managed subscription policy), so every run fails before the model. Allowed: "
            f"{', '.join(allowed) or 'none'}. Fix: {fix}.")


def job_pin_issue(job: dict, cfg: Optional[dict]) -> Optional[str]:
    """Rejection message for the model this job would run on, or None."""
    if job.get("no_agent") or cfg is None:
        return None
    model, source = effective_model(job, cfg)
    provider = job.get("provider") if source == "job" else None
    allowed = rejected_pin(model, provider, cfg)
    if allowed is None:
        return None
    return rejection_message(model, provider, allowed, job_id=job.get("id"), source=source)


def load_config() -> dict:
    from hermes_cli.config_effective import load_user_config_effective
    return load_user_config_effective()
