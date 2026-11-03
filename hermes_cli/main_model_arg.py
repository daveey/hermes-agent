"""``hermes -m`` argument checks run at startup, before any API call (:mod:`hermes_cli.main`). [daveey]"""
import sys


def reject_startup_spaced_model(explicit_model: str, explicit_provider: str, model_cfg: dict) -> None:
    """``-m`` gets the ``/model`` whitespace rule before any API call [daveey].

    A shell loop that fails to split a list passes every id as one ``-m`` value; a cloud provider
    then 404s it (or a fallback chain answers on a different model and the probe "passes").
    The configured base_url only counts when it belongs to the provider being used.
    """
    if not explicit_model or not any(ch.isspace() for ch in explicit_model):
        return
    cfg_provider = str(model_cfg.get("provider") or "").strip()
    provider = explicit_provider or cfg_provider
    base_url = str(model_cfg.get("base_url") or "").strip()
    if explicit_provider and explicit_provider != cfg_provider:
        base_url = ""
    from hermes_cli.models_validate import model_whitespace_error

    if model_whitespace_error(explicit_model, provider, base_url or None):
        sys.stderr.write(
            f"Model names cannot contain spaces. Pass one model per run.\n  got: -m {explicit_model!r}\n"
        )
        raise SystemExit(2)
