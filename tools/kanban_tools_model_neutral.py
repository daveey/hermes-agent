"""Model-neutral create validation before publication or courier work."""
from functools import wraps

from hermes_cli.kanban_model_neutral import validate_selector, validate_text


def model_neutral_create(fn):
    @wraps(fn)
    def create(args, **kwargs):
        from tools.registry import tool_error
        try:
            validate_selector(args.get('model'), args.get('provider'), mandatory=args.get('pin_mandatory'))
            validate_text(args.get('title'), args.get('body'))
        except ValueError as exc:
            # Only pure pre-dispatch validation proves that no creation occurred.
            # Couriers must not retry this immutable request as an unknown write.
            return tool_error(str(exc), code='kanban_model_restriction', created=False,
                              status='rejected', handoff_complete=False)
        return fn(args, **kwargs)
    return create


def model_neutral_review(fn):
    """Opt-in review-metadata guard before worker guard, goal gate, source registration and staging.

    Ordinary ``tool_error`` refusal (no typed code); metadata flags keep base truthiness (N1).
    """
    @wraps(fn)
    def review(args, **kwargs):
        from hermes_cli.kanban_model_neutral import validate_explicit_selector, validate_review
        from tools.registry import tool_error
        metadata = args.get('metadata')
        if isinstance(metadata, dict):
            try:
                validate_explicit_selector(
                    metadata.get('review_model'), metadata.get('review_provider'),
                    mandatory=metadata.get('pin_mandatory') or metadata.get('require_model'))
                validate_review(metadata)
            except ValueError as exc:
                return tool_error(str(exc))
        return fn(args, **kwargs)
    return review
