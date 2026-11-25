"""Durable diagnostic for rejected worker creates; never forwarding authority. [daveey]"""
from __future__ import annotations

import hashlib
from pathlib import Path


def publish(request: dict, *, home: Path, gateway: str, profile: str) -> None:
    """A terminal receipt is safe only after the actual origin chat has this notice.

    The trusted local courier supplies the server-stamped envelope. Revalidate its
    local route and use the canonical lineage resolver; never create a chat or an
    origin binding. Busy/compressing stores retain the courier's pending retry.
    """
    from hermes_cli.kanban_origin_adapter import _resolve_session
    from hermes_cli.kanban_origin_forward_decision import _canonical, _validated
    from hermes_state import SessionDB
    from hermes_state_errors import CompressionSessionClosedError, SessionTurnLeaseLostError
    frozen = _validated(request, gateway, profile)
    origin = frozen['args']['origin']
    try:
        try:
            target = _resolve_session(home / 'state.db', origin['session'])
        except ValueError as exc:
            # A lineage can change after admission's existence check. Do not
            # finalize a silent rejection when its destination becomes unreadable.
            raise ValueError('origin_forward_notice_pending') from exc
        with SessionDB(home / 'state.db') as db:
            session = db.get_session(target)
            if not session or (session.get('profile_name') or profile) != profile:
                raise ValueError('origin_forward_notice_pending')
            creator = frozen['args']['creator']
            source = '/'.join(creator[k] for k in ('gateway', 'profile', 'board', 'task_id'))
            text = (f"Kanban child was not created: origin_forward_binding_missing. "
                    f"Request {frozen['id']}; source {source}. "
                    "This gateway has no accepted origin binding for that source. "
                    "The owning chat must inspect this exact request and resolve source custody "
                    "through the supported ownership path before submitting new work; do not blindly retry creation. "
                    "No execution, scope renewal or acceptance was granted.")
            identity = hashlib.sha256(_canonical(frozen).encode()).hexdigest()
            _, inserted = db.record_kanban_notice(target, text, {
                'notice_id': 'origin-forward-rejected:' + identity,
                'display_text': text,
            })
            if inserted:
                from hermes_state_outcome import refresh_outcomes_quietly
                refresh_outcomes_quietly([target], 'notice', db=db)
    except (CompressionSessionClosedError, SessionTurnLeaseLostError) as exc:
        raise ValueError('origin_forward_notice_pending') from exc
