"""Read-only source attribution for private restriction diagnostics. [daveey]

Recorded routes do not prove liveness; recovery instructions confer no release
permission. The existing held classifier and guarded home release remain authoritative.
"""
from __future__ import annotations

import shlex

from hermes_cli.kanban_actionability_private import DECISIONS, RELEASE, RENEWAL, STOPS, _payload, held


def _context(events, source):
    """Only source-specific resolution evidence; aggregate uncertainty is handled by held()."""
    for event in events:
        if event['kind'] in DECISIONS:
            yield event
        payload = _payload(event) or {}
        if event['kind'] == RELEASE and payload.get('episode') == source:
            yield event
        if event['kind'] == RENEWAL:
            covered = payload.get('covered_restriction_uids')
            if isinstance(covered, list) and source in covered:
                # A valid multi-source renewal contributes only this source's mandate.
                # Keep malformed targeted evidence intact so held() still refuses it.
                if all(isinstance(uid, str) and uid for uid in covered):
                    event = {**event, 'payload': {**payload, 'covered_restriction_uids': [source]}}
                yield event


def details(task, reader, events, verified, releases):
    """Name every unresolved stop, including history hidden by a newer stop's deadline."""
    from hermes_cli.kanban_actionability_sources import _administration

    source_events = reader.events(task['id'])
    out = []
    for event in events:
        if event['kind'] not in STOPS:
            continue
        # Triage itself is not source attribution. Classify the exact stop against
        # the unchanged verified resolution evidence, without the status shortcut.
        reason = held('ready', [event, *_context(events, event['uid'])],
                      verified_cover=verified, verified_releases=releases)
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
