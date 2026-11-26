"""Typed non-running explanations; display evidence, NEVER an admission decision. [daveey]

Unknown producer codes remain unknown. In particular an unexplained failed claim
is neither a Board refusal nor proof of a worker-capacity wait. All prose is fixed
here; caller-controlled diagnostics/commands are not exposed to the renderer.
"""
from __future__ import annotations

NONRUNNING_STATUSES = frozenset(('ready', 'todo', 'scheduled', 'blocked', 'triage', 'review'))

# family, disposition, next actor, explanation, next action/wake predicate
CLAIMS = {
    'usage_accounting_pending': ('accounting', 'blocked', 'agent', 'Run usage accounting is unsettled', 'Recover the original source accounting evidence, then retry normal admission'),
    'decision_pending': ('decision', 'blocked', 'owner', 'An execution decision is pending', 'The existing decision owner must resolve the recorded hold'),
    'spend_budget_exhausted': ('spend', 'blocked', 'owner', 'The execution spend budget is exhausted', 'The existing budget decision owner must resolve the stop'),
    'spend_policy_unavailable': ('spend', 'blocked', 'agent', 'Spend policy is unavailable', 'Restore readable spend policy before normal admission'),
    'capacity': ('capacity', 'waiting', 'dispatcher', 'Waiting for worker capacity', 'The dispatcher retries when a worker slot becomes available'),
    'waiting_for_worker_capacity': ('capacity', 'waiting', 'dispatcher', 'Waiting for worker capacity', 'The dispatcher retries when a worker slot becomes available'),
    'lease_held': ('lease', 'waiting', 'dispatcher', 'Another executor holds the task lease', 'Wait for the current lease to settle or expire'),
    'offline': ('lease', 'blocked', 'agent', 'The Board lease service is unavailable', 'Restore Board connectivity before normal admission'),
    'denied': ('lease', 'blocked', 'agent', 'The Board refused the task lease', 'Inspect the authoritative lease refusal before retrying'),
    'labels_mismatch': ('placement', 'blocked', 'agent', 'No matching execution placement on this worker', 'The placement owner must supply an eligible executor'),
    'pinned_only': ('placement', 'blocked', 'agent', 'This worker cannot claim the requested placement', 'The placement owner must supply an eligible executor'),
    'not_claiming': ('placement', 'blocked', 'agent', 'The requested executor is not claiming tasks', 'Restore the executor or resolve its existing hold'),
    'containment_held': ('containment', 'blocked', 'owner', 'Execution is contained', 'The existing containment authority must resolve the recorded episode'),
    'gateway_draining': ('drain', 'waiting', 'dispatcher', 'The gateway is draining', 'Wait for the supervised gateway drain to finish'),
    'worker_draining': ('drain', 'waiting', 'dispatcher', 'The worker is draining', 'Wait for the supervised worker drain to finish'),
    'update_admission_hold': ('drain', 'waiting', 'dispatcher', 'An update admission hold is active', 'Wait for the supervised update hold to clear'),
    'update_admission_busy': ('contention', 'waiting', 'dispatcher', 'Update admission is contended', 'The dispatcher retries after the admission lock is released'),
    'update_admission_unavailable': ('drain', 'blocked', 'agent', 'Update admission evidence is unavailable', 'Restore valid update admission evidence'),
    'turn_admission_waiting': ('contention', 'waiting', 'dispatcher', 'A chat turn has admission priority', 'The dispatcher retries after the waiting turn is admitted'),
}
PREFIXES = {
    'hub_cpu': ('resources', 'waiting', 'dispatcher', 'The hub CPU admission gate is holding execution', 'Wait for an admissible hub sample or eligible worker'),
    'hub_admission': ('resources', 'blocked', 'agent', 'Hub admission evidence is unavailable', 'Restore authoritative hub admission evidence'),
    'hub_capacity': ('capacity', 'waiting', 'dispatcher', 'Waiting for hub execution capacity', 'The dispatcher retries when capacity is available'),
    'worker_resources': ('resources', 'blocked', 'agent', 'Worker resource evidence prevents admission', 'Restore eligible worker resources and fresh evidence'),
    'worker_disk': ('storage', 'blocked', 'agent', 'Worker disk admission prevents execution', 'Restore the existing disk floor without weakening admission'),
    'disk': ('storage', 'blocked', 'agent', 'Disk admission prevents execution', 'Restore the existing disk floor without weakening admission'),
    'containment': CLAIMS['containment_held'],
}
BUCKETS = {
    'skipped_ineligible': ('placement', 'blocked', 'agent', 'This executor is ineligible', 'The placement owner must supply an eligible executor'),
    'skipped_unassigned': ('assignment', 'blocked', 'owner', 'No execution profile is assigned', 'Assign an existing eligible execution profile'),
    # The predicate includes external control-plane lanes AND per-home profile
    # allowlists. It establishes neither broken startup nor a fleet-wide wall.
    'skipped_nonspawnable': ('assignment', 'unknown', 'unknown', 'The assigned execution lane is not available on this dispatcher', 'Inspect the assigned lane and its executor; task-wide execution eligibility is unknown'),
    'capsule_refused': ('capability', 'blocked', 'agent', 'Required execution capability is unavailable', 'Restore the required capability without bypassing admission'),
    'no_progress_held': ('decision', 'blocked', 'owner', 'Execution is held by its progress policy', 'The existing decision owner must resolve the progress hold'),
    'per_profile_capped': CLAIMS['capacity'],
    'quiet_deferred': ('pacing', 'waiting', 'dispatcher', 'The automatic claim quiet window is active', 'Wait for the recorded quiet window to expire'),
    'owner_down': ('startup', 'blocked', 'agent', 'The execution owner is unavailable', 'Restore the existing execution owner'),
    'pinned_quota_wall': ('quota', 'waiting', 'dispatcher', 'The requested model class is quota held', 'Wait for the recorded provider reset'),
    'llm_paced': ('pacing', 'waiting', 'dispatcher', 'Model usage is paced', 'Wait for the existing pacing gate to admit execution'),
    'review_provider_wall': ('review', 'waiting', 'dispatcher', 'Review execution is provider held', 'Wait for the recorded review provider reset'),
    # A callback holds this dispatcher's next start, not task-wide execution.
    # Plugin detail words are not a trusted actor, deadline or display message.
    'start_gate': ('plugin', 'waiting', 'dispatcher', 'A plugin start gate is holding this start', 'The dispatcher rechecks the plugin gate on the next dispatch pass'),
    'memory_pressure': ('resources', 'waiting', 'dispatcher', 'The worker is under memory pressure', 'Wait for admissible memory headroom'),
    'update_drain': CLAIMS['update_admission_hold'],
    'at_capacity': CLAIMS['capacity'],
}


def describe(reason, detail=None):
    """Project real producer tokens, not English substring guesses or an invented actor."""
    code = detail if reason == 'claim_deferred' else reason
    rule = (CLAIMS.get(code) if isinstance(code, str) else None) if reason == 'claim_deferred' else BUCKETS.get(reason)
    if code in ('offline', 'denied', 'lease_held') and getattr(detail, 'origin', None) != 'board':
        rule = None  # the token alone does not establish a Board-origin refusal
    if rule is None and reason == 'claim_deferred' and isinstance(code, str):
        rule = next((value for prefix, value in PREFIXES.items()
                     if code == prefix or code.startswith(prefix + '_') or code.startswith(prefix + ':')), None)
    # Producer details may be provenance-bearing str subclasses. Preserve their
    # origin for classification above, but expose only plain wire values: copying
    # a ClaimDeferral would reconstruct it without its required origin argument.
    code = str(code) if isinstance(code, str) else code
    if rule is None:
        return {'code': code if isinstance(code, str) and code.isidentifier() else 'unclassified_refusal',
                'family': 'unknown', 'disposition': 'unknown', 'actor': 'unknown',
                'summary': 'Execution reason is unknown', 'next_action': 'Inspect the latest authoritative dispatcher evidence'}
    family, disposition, actor, summary, action = rule
    return dict(code=code if isinstance(code, str) and code.isidentifier() else reason,
                family=family, disposition=disposition, actor=actor, summary=summary, next_action=action)


def from_result(result, tid, *, update_drain=False):
    """Use decisions actually made this pass, never re-run an admission policy."""
    from hermes_cli.kanban_dispatch_skip import _tick_skips
    found = [describe(reason, detail) for task, reason, detail, _until, _extra in _tick_skips(result) if task == tid]
    for bucket in ('skipped_unassigned', 'skipped_nonspawnable'):
        if tid in getattr(result, bucket, ()):
            found.append(describe(bucket))
    if any(task == tid for task, _ in getattr(result, 'quiet_deferred', ())):
        found.append(describe('quiet_deferred'))
    # A task-specific refusal must not be flattened by a concurrent global capacity cap.
    if not found:
        if update_drain:
            found.append(describe('update_drain'))
        elif getattr(result, 'memory_pressure', None):
            found.append(describe('memory_pressure'))
        elif getattr(result, 'at_capacity', None):
            found.append(describe('at_capacity'))
    if not found:
        return None  # This pass did not explain this task. No implied queue/capacity claim.
    for item in found:
        # These gates describe this observer, not every eligible machine. Even
        # task-specific local placement checks cannot certify a fleet-wide wall.
        scoped = (item['code'] in ('skipped_nonspawnable', 'start_gate')
                  or item['family'] in ('capacity', 'resources', 'storage', 'drain', 'placement', 'contention'))
        item['scope'] = 'observing_dispatcher' if scoped else 'task'
        if scoped:
            item['summary'] = 'Observing dispatcher: ' + item['summary']
    kinds = {item['disposition'] for item in found}
    if 'blocked' in kinds:
        return next(item for item in found if item['disposition'] == 'blocked')
    if 'unknown' in kinds:
        return next(item for item in found if item['disposition'] == 'unknown')
    return found[0]


def snapshot(conn):
    """One bounded latest local observation; no write, clock renewal or replica inference."""
    import json
    import sqlite3
    from hermes_cli.execution_observations import epoch, ident
    try:
        row = conn.execute('SELECT payload FROM kanban_capacity_observation WHERE id=1').fetchone()
        value = json.loads(row[0]) if row and len(row[0]) <= 1024 * 1024 else None
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('tasks'), dict):
            return None
        if not all(ident(value.get(k)) for k in ('board', 'machine', 'source_revision', 'evidence_ref')):
            return None
        if not all(epoch(value.get(k)) for k in ('observed_at_ms', 'valid_until_ms')):
            return None
        return value
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _unknown_reason(code, *, at=None, until=None):
    return {**describe('unclassified'), 'freshness': 'unknown', 'unknown_reason': code,
            'observed_at': at, 'valid_until': until}


def _current_reason(row, source):
    """A diagnostic has its own clock; a queued phase does not attest admission."""
    from hermes_cli.execution_observations import epoch
    snap = source.get('dispatch_observation')
    if not isinstance(snap, dict) or snap.get('machine') != source.get('gateway') or snap.get('board') != source.get('board'):
        return _unknown_reason('dispatcher_evidence_missing')
    stored = snap.get('tasks', {}).get(row['id'])
    lifecycle = stored.get('lifecycle') if isinstance(stored, dict) else None
    if not isinstance(lifecycle, dict):
        return _unknown_reason('dispatcher_evidence_missing')
    if any(lifecycle.get(key) != row.get(field) for key, field in
           (('status', 'status'), ('run_id', 'current_run_id'), ('assignee', 'assignee'), ('runs_on', 'runs_on'))):
        return _unknown_reason('dispatcher_lifecycle_changed')
    watermark = stored.get('source_event')
    # A truncated first-page history cannot prove the absence of a later event.
    if type(watermark) is not int or source.get('event_watermark') != watermark:
        return _unknown_reason('dispatcher_lifecycle_changed')
    at, until, now = snap.get('decision_at_ms'), snap.get('valid_until_ms'), source['observed_at']
    if not all(epoch(t) for t in (at, until, now)):
        return _unknown_reason('dispatcher_evidence_missing')
    at, until = at / 1000, min(until / 1000, at / 1000 + 180)
    if not at <= now < until:
        return _unknown_reason('dispatcher_evidence_stale', at=at, until=until)
    reason = stored.get('non_running_reason')
    if not isinstance(reason, dict):
        return _unknown_reason('dispatcher_reason_unrecorded', at=at, until=until)
    return {**reason, 'observer': snap['machine'], 'freshness': 'current', 'observed_at': at, 'valid_until': until,
            'evidence_ref': snap['evidence_ref'], 'source_revision': snap['source_revision']}


def enrich(value, row, source):
    """Retain lifecycle, execution identity and phase clocks unchanged.

    Diagnostics are not execution authority: a missing phase generation remains
    missing even when this pass explains a refusal. Running, human and exact
    schedule/dependency/review waits keep their existing independent contracts.
    """
    if row.get('status') not in NONRUNNING_STATUSES or row.get('current_run_id') is not None:
        return value
    if row.get('status') != 'ready':
        # Native waits keep their own contracts. A ready refusal cannot explain
        # a replacement parked lifecycle, even at the same task coordinates.
        return {**value, 'non_running_reason': _unknown_reason('lifecycle_reason_unobserved')}
    local = _current_reason(row, source)
    remote = source.get('remote_non_running_reason')
    if isinstance(remote, dict) and remote.get('unknown_reason') == 'dispatcher_evidence_conflict':
        # Qualified contradictory observers are not simple remote absence. A
        # separate local observation cannot turn that contradiction into fact.
        return {**value, 'non_running_reason': remote}
    if isinstance(remote, dict) and remote.get('freshness') == 'current':
        if local.get('freshness') == 'current' and (local['code'], local['disposition']) != (remote['code'], remote['disposition']):
            local = _unknown_reason('dispatcher_evidence_conflict')
        elif local.get('freshness') != 'current':
            local = remote
    return {**value, 'non_running_reason': local}


def select(records, row, *, now):
    """Select independently acquired diagnostics, NOT execution or writer authority.

    Acquisition must already bind/authenticate each record. A task-scoped reason
    needs no run generation. Every scope component must match; newer unknown or
    conflicting observations cannot revive an older refusal. Never mutates input.
    """
    from hermes_cli.execution_observations import epoch, ident, nonrunning_reason
    if row.get('status') not in NONRUNNING_STATUSES or row.get('current_run_id') is not None:
        return None
    if row.get('status') != 'ready':
        # Deliberately do not inspect a prior ready dispatch carrier. A current
        # board status is not a fresh observation of this lifecycle's cause.
        return _unknown_reason('lifecycle_reason_unobserved')
    unknown = _unknown_reason('dispatcher_evidence_missing')
    if row.get('stale'):
        return _unknown_reason('source_unavailable')
    wanted = {'kind': 'card', 'work_id': row.get('id'),
              **{k: row.get(k) for k in ('gateway', 'profile', 'board')}}
    if not all(ident(v) for v in wanted.values()) or not isinstance(records, list):
        return unknown
    candidates = []
    for record in records:
        if not isinstance(record, dict) or record.get('version') != 1:
            continue
        identity, owner = record.get('identity'), record.get('owner')
        if not isinstance(identity, dict) or any(identity.get(k) != v for k, v in wanted.items()):
            continue
        if not isinstance(owner, dict) or any(owner.get(k) != wanted[k] for k in ('gateway', 'profile')):
            return _unknown_reason('dispatcher_scope_conflict')
        state = record.get('state')
        at, until = record.get('observed_at'), record.get('valid_until')
        if (state in ('running', 'done', 'failed', 'cancelled', 'needs_user')
                and all(epoch(t) for t in (at, until, now)) and at <= now < min(until, at + 180)):
            return _unknown_reason('dispatcher_phase_conflict')
        reason = record.get('non_running_reason')
        if isinstance(reason, dict):
            candidates.append(nonrunning_reason(reason, now=now))
    if not candidates:
        return unknown
    # Undated evidence cannot establish an ordering relative to dated evidence.
    if any(not epoch(c.get('observed_at')) for c in candidates):
        return _unknown_reason('dispatcher_order_unknown')
    newest = max(c['observed_at'] for c in candidates)
    latest = [c for c in candidates if c['observed_at'] == newest]
    if any(c != latest[0] for c in latest[1:]):
        return _unknown_reason('dispatcher_evidence_conflict')
    return latest[0]

