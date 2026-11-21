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
            return tool_error(str(exc))
        return fn(args, **kwargs)
    return create
