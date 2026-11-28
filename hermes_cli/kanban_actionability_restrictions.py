"""Read-only source attribution for private restriction diagnostics. [daveey]

Recorded routes do not prove liveness; recovery instructions confer no release
permission. The existing held classifier and guarded home release remain authoritative.
"""
from __future__ import annotations

import shlex

from hermes_cli.kanban_actionability_private import STOPS, _payload, held


def details(task, reader, events, verified, releases):
    """Name every unresolved stop, including history hidden by a newer stop's deadline."""
    from hermes_cli.kanban_actionability_sources import _administration

    context = [e for e in events if e['kind'] not in STOPS]
    source_events = reader.events(task['id'])
    out = []
    for event in events:
        if event['kind'] not in STOPS:
            continue
        # Triage itself is not source attribution. Classify the exact stop against
        # the unchanged verified resolution evidence, without the status shortcut.
        reason = held('ready', [event, *context], verified_cover=verified, verified_releases=releases)
        if reason is None:
            continue
        payload = _payload(event) or {}
        cause = payload.get('cause')
        admin, _ = _administration(reader, source_events, event['uid'])
        command = ['hermes', 'kanban', 'release', task['id']]
        capability = cause == 'capability_unavailable' and event['kind'] == 'spend_budget_triaged'
        if capability:
            command += ['--capability-episode', event['uid']]
        release = {'authority': 'unavailable', 'condition':
                   'Recover source custody through the owning chat/operator; preserve all independent holds.'}
        if event['kind'] == 'spend_budget_triaged' and cause in (None, 'scope_stopped', 'capability_unavailable') and reason not in (
                'safety_restriction', 'private_payload_unverifiable', 'renewal_unverifiable'):
            release = {'authority': 'existing_lifecycle_gates', 'condition':
                       'Owning chat or human/delegate operator reviews this exact source, then uses '
                       f'`{shlex.join(command)}` on the home gateway '
                       f"({task.get('created_on') or 'resolve home custody first'}). "
                       'Select all unresolved capability episodes together; renew the current scope through '
                       'the guarded release flow. This diagnostic is not release authority or admission.'}
            if capability:
                release['capability_episode'] = event['uid']
        out.append({'source_uid': event['uid'], 'kind': event['kind'], 'cause': cause,
                    'reason': reason, 'owner': admin, 'home_gateway': task.get('created_on'),
                    'release': release, 'evidence_state': 'unknown' if reader.synced else 'known'})
    return out
