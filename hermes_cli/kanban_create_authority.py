"""Transactional creation authority for opt-in agent/CLI ingress. [daveey]

A creator name or bare creator-task ID cannot renew scope. Require the actual
renew-scope actor predicate, or persist a registered creating chat's anchor in
this same transaction. Internal/system creators do not opt in. No network,
lifecycle transitions, or authority inferred from ``created_by``.
"""
from __future__ import annotations

from hermes_cli.kanban_adoption_authority import gateway as _gateway, released_upstream as _released_upstream

CODE = "scope_authority_missing"


class CreateAuthorityRefused(ValueError):
    """The create has no renewable creator-task or owning-chat authority."""
    code = CODE


def _accepts(conn, task_id, session, gateway, profile, creator=None):
    """Use the renewal reader itself, including its own-run and creator-event fences.

    These are candidate identities from stored evidence, not authentication of
    this process and not a renewal. The eventual caller must prove its ancestry.
    """
    from hermes_cli.kanban_scope_renewal_cli import resolve_actor
    from hermes_cli.kanban_scope_renewal import RenewalRefused
    env = {"HERMES_SESSION_ID": session}
    if creator:
        env["HERMES_KANBAN_TASK"] = creator
    try:
        resolve_actor(conn, task_id, writer=("agent", session, "1:HERMES_SESSION_ID"),
                      environs=[env], gateway=gateway, profile=profile)
    except RenewalRefused:
        return False
    return True


def has_authority(conn, task_id: str, creator_task_id, *, durable: bool = False) -> bool:
    """Read-only: some recorded candidate can pass the real renewal authority reader.

    ``durable=True`` ignores the creator-task path: it holds only while the creator is
    running, so once the creator completes nothing could renew. Only a chat link/sub
    (inherited from an authentic creating chat, or the caller's own) survives that.

    Machine sessions cannot own chats. A qualified foreign hash needs the same
    durable provenance as creator-chat inheritance; observer/released links and
    placeholder IDs never establish ownership. Legacy subscriptions must match
    the renewal reader exactly (do not strip a foreign qualified destination).
    """
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_scope_renewal_cli import _sessions
    from hermes_cli.kanban_session_provenance import proven_foreign_chats
    from hermes_cli.profiles import get_active_profile_name

    me, profile = _gateway(conn), get_active_profile_name()
    if creator_task_id and not durable:
        for session in _sessions(conn, creator_task_id):
            if session.strip() and not is_placeholder_session(session) and _accepts(
                    conn, task_id, session, me, profile, creator_task_id):
                return True
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        proven = None
        for row in _chat_links(conn, task_id):
            session = row["chat_session"]
            if not session or not session.strip() or row.get("kind", "created") != "created" \
                    or is_placeholder_session(session):
                continue
            gateway, chat_profile = row["chat_gateway"], row["chat_profile"]
            if is_machine_session(gateway or None, session, local_gateway=me, profile=chat_profile):
                proven = proven if proven is not None else proven_foreign_chats(conn, (task_id,), local_gateway=me)
                profile_ref = chat_profile if chat_profile is not None else "default"
                if f"{gateway}/{profile_ref}/{session}" not in proven:
                    continue
            if _accepts(conn, task_id, session, gateway, chat_profile or "default"):
                return True
    else:
        for (session,) in conn.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id=?", (task_id,)):
            # _owning_sub uses exact equality with the caller's bare session ID.
            if session and "/" not in session and session.strip() and not is_placeholder_session(session) \
                    and not is_machine_session(me or None, session, local_gateway=me, profile=profile) \
                    and _accepts(conn, task_id, session, me, profile):
                return True
    return False


def _anchor_registered_session(conn, task_id):
    """Persist a proven local creating chat, rather than waiting for link reconciliation.

    Front doors stamp registered sessions but subscribe only AFTER create commits.
    A raw unknown ID is not proof, even when it does not look like a machine ID.
    This helper runs under create's existing transaction; no schema changes.
    """
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_session_provenance import scoped_sources
    from hermes_cli.profiles import get_active_profile_name

    row = conn.execute("SELECT session_id, created_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    session = row[0] if row else None
    if not session or not session.strip() or "/" in session or is_placeholder_session(session):
        return
    me, profile = _gateway(conn), get_active_profile_name()
    if _released_upstream(conn, task_id, session):
        return  # an inherited projection of a chat that gave up ownership is not authority
    # A standalone (never-enrolled) board has no gateway name, but its local store
    # still proves a registered chat. None means unqualified LOCAL, not a foreign scope.
    if not scoped_sources(me or None, (session,), local_gateway=me, profile=profile).get(session) \
            or is_machine_session(me or None, session, local_gateway=me, profile=profile):
        return
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        # Match the legacy tasks.session_id projection's default-profile UID so
        # ordinary reconciliation does not create a second owning link.
        chat_profile = None if profile == "default" else profile
        conn.execute(
            "INSERT OR IGNORE INTO task_chat_links "
            "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
            "VALUES (?,?,?,?,?,'created',0,?)",
            (link_uid(task_id, me, chat_profile, session, "created"), task_id, me, chat_profile, session, row[1]))
        # Reconciliation preserves profile-qualified created links only with durable
        # resolution provenance. A bare tasks.session_id would independently project
        # a default-profile owner, even if that same ID belongs to a different chat.
        from hermes_cli.kanban_db import _append_event
        _append_event(conn, task_id, "creator_chats_resolved",
                      {"registered_session": session, "chat_gateway": me, "chat_profile": chat_profile})
        if profile != "default":
            conn.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (task_id,))
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs "
        "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
        "VALUES (?,'tui',?,'','notify','{\"reason\":\"create\"}',?, "
        "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?))",
        (task_id, session, row[1], task_id))


def require(conn, task_id: str, creator_task_id) -> None:
    # A running creator alone is transient authority; require a chat that outlives it.
    if has_authority(conn, task_id, creator_task_id, durable=True):
        return
    _anchor_registered_session(conn, task_id)
    if not has_authority(conn, task_id, creator_task_id, durable=True):
        raise CreateAuthorityRefused(
            "this card would have no renewable scope authority. Create it from a registered owning chat, "
            "pass --origin-session with an unreleased created chat anchor, or create it from a creator "
            "task that descends from a registered owning chat (a running creator alone ends with its run). "
            "A creator name, bare task ID, placeholder or observer "
            "subscription is not authority; nothing was created.")
