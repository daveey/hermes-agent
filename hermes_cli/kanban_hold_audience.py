"""Read-only renewal authority for decision notices; subscriptions are not grants. [daveey]"""
from __future__ import annotations


def describe(conn, task_id: str) -> dict:
    """Describe only stored candidates accepted by the real renewal actor reader.

    This is routing evidence, not authentication or a release receipt. The eventual
    caller must still prove its process ancestry and pass canonical revalidation.
    Legacy observer-only cards remain owner-unavailable; never adopt them here.
    """
    from hermes_cli.kanban_archive_scope import _creator_task_id
    from hermes_cli.kanban_create_authority import _accepts, _gateway
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_scope_renewal_cli import _sessions
    from hermes_cli.profiles import get_active_profile_name
    own, profile = _gateway(conn), get_active_profile_name()
    candidates = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        candidates = [(r['chat_gateway'], r.get('chat_profile') or 'default', r['chat_session'])
                      for r in _chat_links(conn, task_id) if r.get('kind', 'created') == 'created']
    else:
        candidates = [(own, profile, r[0]) for r in conn.execute(
            "SELECT chat_id FROM kanban_notify_subs WHERE task_id=? AND platform='tui'", (task_id,))]
    from hermes_cli.kanban_session_provenance import proven_foreign_chats
    chats, proven = [], None
    for gateway, prof, session in dict.fromkeys(candidates):
        if not session:
            continue
        if is_machine_session(gateway, session, local_gateway=own, profile=prof):
            # A foreign Desktop's local-<hash> cannot be inspected in our state.db.
            # Match creation's provenance exception, then still validate renewal.
            proven = proven if proven is not None else proven_foreign_chats(conn, (task_id,), local_gateway=own)
            if f'{gateway}/{prof}/{session}' not in proven:
                continue
        if _accepts(conn, task_id, session, gateway, prof):
            chats.append({'gateway': gateway, 'profile': prof, 'session': session})
    creator = _creator_task_id(conn, task_id)
    active_creator = bool(creator and any(_accepts(conn, task_id, s, own, profile, creator)
                                         for s in _sessions(conn, creator)))
    return {'chats': chats, 'creator_task_id': creator, 'creator_active': active_creator}


def chats(authority) -> list[dict]:
    """Validate a replicated authority snapshot without accepting partial identities."""
    values = authority.get('chats') if isinstance(authority, dict) else None
    return [c for c in values if isinstance(c, dict)
            and all(isinstance(c.get(k), str) and c[k].strip() for k in ('gateway', 'profile', 'session'))
            ] if isinstance(values, list) else []


def guidance(authority: dict) -> str:
    """Never imply an observer can execute a renewal just because it got this notice."""
    refs = [f"@session:{c['profile']}@{c['gateway']}/{c['session']}" for c in chats(authority)]
    if isinstance(authority, dict) and authority.get('creator_active') is True and authority.get('creator_task_id'):
        refs.append('running creator task ' + str(authority['creator_task_id']))
    if refs:
        return 'Recorded renewal authority: ' + ', '.join(refs) + '. Observer recipients have no renewal authority.'
    return ('No recorded renewable agent authority. This legacy card remains held; an observer cannot renew it. '
            'Ownership must be resolved through the supported operator authority path, not inferred from '
            'notification delivery. No scope was renewed and no execution was authorized.')
