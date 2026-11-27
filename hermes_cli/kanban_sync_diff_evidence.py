"""Detached inputs from an opt-in reserved sync report, never an exemption. [daveey]

Only refusal.report calls finish, inside its existing zero-wait reservations.
Hashes/serialization belong after the caller's final feed/generation/freshness
validation. These raw observations grant no historical or execution authority.
"""
from __future__ import annotations

from hermes_cli import kanban_db_sync as sync


def finish(live, shadow, gateway, evidence):
    """Add explicit per-op fresh verdict/custody and state to exact diff inputs."""
    from hermes_cli import kanban_sync_actionability_drift as diagnostic
    from hermes_cli import kanban_actionability_transport as action_transport
    from hermes_cli import kanban_retain_transport as retain_transport

    audits = []
    for op in evidence['rejected']:
        fresh = None
        custody = None
        if op['op'] == 'actionability_record':
            fresh = diagnostic.audited_rejection(live, shadow, op, fresh_gateway=gateway)
            custody = action_transport.command(live, op['op_id'])
        elif op['op'] == 'retain':
            custody = retain_transport.command(live, retain_transport.frozen_wire(op).get('run_uid'))
        audits.append({'op_id': op['op_id'], 'ordinary': evidence['ordinary_audits'][op['op_id']],
                       'diagnostic': fresh, 'custody': custody})
    evidence['audits'] = audits
    evidence['state'] = sync.sync_state(shadow)
    evidence['outbox_pending'] = live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0]
