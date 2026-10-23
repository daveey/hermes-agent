"""Pure credential row normalization and ordering helpers."""


def normalize_pool_auth_type(provider, token, auth_type):
    from agent.credential_pool import AUTH_TYPE_API_KEY, AUTH_TYPE_OAUTH

    if provider == "anthropic" and isinstance(token, str) and token.startswith("sk-ant-oat"):
        return AUTH_TYPE_OAUTH
    return str(auth_type or AUTH_TYPE_API_KEY)


def next_priority(entries):
    return max((entry.priority for entry in entries), default=-1) + 1
