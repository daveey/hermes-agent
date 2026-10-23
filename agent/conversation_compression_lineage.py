"""Persisted identity carried across compression continuations."""
import contextlib


def compression_child_source(agent, parent_session_id):
    """Keep the parent's persisted source, including tool/oneshot/kanban labels."""
    parent = None
    with contextlib.suppress(Exception):
        parent = agent._session_db.get_session(parent_session_id)
    if parent and parent.get("source"):
        return parent["source"]
    from agent.session_source import session_source_for
    return session_source_for(getattr(agent, "platform", None))
