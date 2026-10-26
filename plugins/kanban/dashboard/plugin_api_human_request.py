"""Trusted actor resolution for audited human-request classification."""
from fastapi import HTTPException, Request


def classification_actor(request: Request) -> str:
    """Use the existing sensitive-endpoint gate, never a payload actor or worker env.

    Dashboard sessions (including the shared loopback token) are trusted operators;
    there is no worker/operator role distinction within that credential. A worker
    holding it has the same authority. Service token principals alone do not pass
    this gate. Annotation records evidence, not approval or OS authentication.
    """
    from hermes_cli.web_server import _require_token

    if request is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    _require_token(request)
    session = getattr(request.state, "session", None)
    if getattr(request.app.state, "auth_required", False):
        assert session is not None  # enforced by the canonical gate above
        return f"dashboard:{session.provider}:{session.user_id}"
    return "dashboard:loopback-token"
