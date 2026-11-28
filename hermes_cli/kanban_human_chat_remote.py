"""Home-gateway coverage for human asks owned by workers, via the origin courier. [daveey]

A worker's task need not have a row in the home's execution board. Discover it in
that board's read-only shadow; never copy the task or write a foreign chat link.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from contextlib import closing

from hermes_cli.worker_drain_fence import fenced

log = logging.getLogger(__name__)


def candidates(conn) -> list[dict]:
    from hermes_cli import kanban_db_sync as sync, kanban_sync_client as client
    from hermes_cli import kanban_human_chat as human
    from hermes_cli.kanban_adoption_authority import gateway
    from hermes_cli.kanban_lockguard import connect
    from hermes_cli.kanban_open_asks import card_is_open_human_ask

    state = sync.sync_state(conn)
    if not state or not human.is_home(conn):
        return []
    board, me = state['board_id'], gateway(conn)
    path = client.shadow_path(board)
    if not path.exists():
        return []
    out = []
    with closing(connect(f'file:{path}?mode=ro', uri=True, timeout=5)) as shadow:
        for tid, owner, status, kind, request in shadow.execute(
                "SELECT id, created_on, status, block_kind, human_request FROM tasks "
                "WHERE status IN ('blocked','triage') AND human_request IS NOT NULL ORDER BY created_at"):
            if not owner or owner == me or not card_is_open_human_ask(status, kind, request):
                continue
            chats = shadow.execute(
                "SELECT chat_gateway,chat_profile,chat_session FROM task_chat_links "
                "WHERE task_id=? AND kind='created' AND released=0", (tid,)).fetchall()
            from hermes_cli.kanban_session_provenance import proven_foreign_chats
            if (any(human.is_human_chat(conn, *row) for row in chats)
                    or proven_foreign_chats(shadow, [tid], local_gateway=me)):
                continue
            from hermes_cli.kanban_origin import _replica_task_owners
            routes = [(gw, profile) for gw, profile in _replica_task_owners(tid, board) if gw == owner]
            if len(routes) != 1:
                raise RuntimeError(f'no unique courier owner route for {owner}/{board}/{tid}')
            out.append({'task': tid, 'gateway': owner, 'profile': routes[0][1], 'board': board})
    return out


@fenced('kanban.human_chat_remote')
def enqueue(card: dict, session: str) -> dict:
    """An idempotent authenticated adoption request, NOT a successful link receipt."""
    from hermes_cli.profiles import get_active_profile_name

    profile = get_active_profile_name()
    target = f"{card['profile']}/{card['board']}/{card['task']}"
    identity = f"{card['gateway']}/{target}/{profile}/{session}"
    rid = 'human-chat-' + hashlib.sha256(identity.encode()).hexdigest()
    result = subprocess.run(
        ['hermes-hub', 'adopt-task', card['gateway'], target, '--origin-session', session,
         '--origin-profile', profile, '--id', rid], capture_output=True, text=True,
        encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError(f'origin adoption enqueue failed: {result.stderr[:500]}')
    return {**card, 'session': session, 'request_id': rid}


def sweep(conn, *, apply: bool, only=None) -> dict:
    from hermes_cli import kanban_human_chat as human
    from hermes_cli.kanban_owner_chat import ensure

    result = {'missing': [], 'pending': [], 'failed': []}
    try:
        cards = candidates(conn)
        if only is not None:
            wanted = set(only)
            cards = [c for c in cards if c['task'] in wanted]
        result['missing'] = cards
        if not apply or not cards:
            return result
        sid = ensure(human.NEEDS_YOU)
        for card in cards:
            try:
                result['pending'].append(enqueue(card, sid))
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                log.warning('kanban: remote human-chat adoption failed for %s', card, exc_info=True)
                result['failed'].append(card)
    except Exception:  # health: allow BLE001 -- maintenance must retry discovery, never hide a failed read
        log.warning('kanban: remote human-ask discovery unavailable', exc_info=True)
        result['failed'].append({'source': 'replica_discovery'})
    return result
