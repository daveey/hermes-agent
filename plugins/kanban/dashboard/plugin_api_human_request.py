"""Trusted actor resolution for audited human-request classification, and the request writes it gates."""
from typing import Optional

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


def classify_request(conn, task_id, payload, actor: str) -> bool:
    from hermes_cli.kanban_human_request_migration import classify_parked_request

    return classify_parked_request(conn, task_id, human_request=payload.human_request,
                                   actor=actor, reason=payload.block_reason)


def check_create_ask(conn, payload) -> None:
    """[daveey] REST create shares filing guards and idempotent-retry semantics with tool/CLI create."""
    from hermes_cli.kanban_ask_resolved import create_context, create_filing_error, idempotent_hit

    status = None if idempotent_hit(conn, payload.idempotency_key) else payload.initial_status
    error = create_filing_error(payload.human_request, status,
                                context=create_context(conn, payload.workspace_path, payload.assignee))
    if error:
        raise HTTPException(status_code=409, detail=error)


def ask_filing_refusal(conn, task_id: str, payload) -> Optional[str]:
    """[daveey] An ask filed with a ``resolved_check`` (block or metadata-only) runs it once first, in the card's
    sweep context, as every other filing path does (``kanban_ask_resolved``): exit 0 or a broken probe refuses."""
    if not (payload.metadata_only or payload.status == "blocked"):
        return None
    from hermes_cli.kanban_ask_resolved import filing_refusal

    hit = filing_refusal(conn, [task_id], payload.human_request)
    return hit[1] if hit else None
