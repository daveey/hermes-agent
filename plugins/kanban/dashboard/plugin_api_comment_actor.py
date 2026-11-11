"""Who wrote a dashboard comment: the trusted actor for ``POST /tasks/{id}/comments``. [daveey]

The Desktop's session-mentions courier serves the agent ``cross_task`` tool through the same
endpoint the Kanban drawer uses, and the endpoint stamped every note ``writer_kind='human'``: an
agent's cross-gateway comment reached the worker as "from the operator" (comment 179201; the
agent-261aa31f49 fix covered only the in-process ``kanban_comment`` tool).

A caller may only LOWER provenance. ``writer_kind='agent'`` (with the sending agent's session) is
accepted. Bare comments from a resolved raw loopback agent process (or inconclusive ancestry)
are also lowered to agent. Clean local ancestry and remote/unresolvable peers keep the legacy
human default. This is a provenance heuristic, not human authentication. An explicit ``human``
claim, an unknown kind, or a session with no agent kind is refused before anything is stored.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel


class CommentBody(BaseModel):
    body: str
    author: Optional[str] = "dashboard"
    # Set only by an agent courier (session-mentions serving ``cross_task``), never by the GUI.
    writer_kind: Optional[str] = None
    writer_session: Optional[str] = None


def comment_writer(payload: CommentBody, scope: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """``(writer_kind, writer_session)`` to record for ``payload``; HTTP 400 for a forged/malformed actor."""
    kind, session = payload.writer_kind, payload.writer_session
    if kind is None:
        if session is not None:
            raise HTTPException(status_code=400, detail="writer_session requires writer_kind='agent'")
        from hermes_cli.kanban_http_comment_writer import http_comment_writer

        return http_comment_writer(scope or {})
    if kind != "agent":
        raise HTTPException(status_code=400, detail=(
            "writer_kind may only be 'agent'; omit it for a comment a person typed "
            "(human provenance is not client-assertable)"))
    return "agent", (session or "").strip()[:200] or None
