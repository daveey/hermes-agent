"""Pure-data defaults for opt-in model-class policy."""


def routing_defaults():
    return {
        "enabled": False,
        "policy_version": 1,
        "allow_metered": False,
        "enforce_subscription_only": False,
        "routes": {"deep": [], "standard": [], "fast": []},
    }
