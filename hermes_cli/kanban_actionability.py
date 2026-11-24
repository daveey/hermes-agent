"""Pure administrative actionability projection; never an admission/release decision. [daveey]"""
from __future__ import annotations

import hashlib
import json
import math

NONTERMINAL = ('todo', 'ready', 'running', 'scheduled', 'triage', 'blocked', 'review')


def positive_time(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return int(value)


def owner(value) -> dict:
    if isinstance(value, str) and value.strip():
        return {'kind': 'assignee', 'refs': [value]}
    return {'kind': 'unavailable', 'refs': []}


def _block(row: dict, prerequisite: str, trigger: str, *, action: str) -> None:
    row.update(action=action, instruction=trigger,
               blocker={'prerequisite': prerequisite, 'trigger': trigger, 'evidence': row['source_refs']})


def _decision(row: dict, facts: dict) -> None:
    contract = facts.get('next_action') or {}
    blocker = contract.get('blocker') if isinstance(contract.get('blocker'), dict) else {}
    prerequisite = blocker.get('prerequisite')
    trigger = blocker.get('trigger') or blocker.get('prerequisite')
    instruction = contract.get('instruction')
    _block(row, prerequisite if isinstance(prerequisite, str) and prerequisite.strip() else (
        facts['stop'].get('cause') or 'Recorded spend decision'),
        trigger if isinstance(trigger, str) and trigger.strip() else
        'Administrative owner records a scoped decision; comments, timers and notices do not release work',
        action='decide_scope')
    if isinstance(instruction, str) and instruction.strip():
        row['instruction'] = instruction
    evidence = blocker.get('evidence')
    if isinstance(evidence, list) and all(isinstance(ref, str) for ref in evidence):
        row['blocker']['evidence'] = list(dict.fromkeys([*row['source_refs'], *evidence]))
    release = contract.get('release')
    if isinstance(release, dict) and isinstance(release.get('condition'), str) and release['condition'].strip():
        row['release']['condition'] = release['condition']
    row['release']['authority'] = 'unavailable'  # recorded conditions never grant release


def _status(row: dict, facts: dict, now: int) -> None:
    status = row['status']
    if status not in NONTERMINAL:
        row.update(action='no_open_action', instruction='n/a: terminal task has no open action')
        row['release'] = {'authority': 'not_applicable', 'condition': 'No open action'}
    elif facts.get('pending_command'):
        _block(row, 'Exact canonical stop receipt reconciliation',
               'Recover the original frozen request and verify its exact receipt; no replacement identity or fallback',
               action='resolve_release_authority')
        row['release']['authority'] = 'unavailable'
        row['evidence_state'] = 'unknown'
    elif facts.get('stop') and (not facts.get('containment') or
                               facts['stop'].get('cause') in ('safety_refusal', 'scope_stopped')):
        _decision(row, facts)
    elif facts.get('containment'):
        _block(row, 'Exact contained episode and live release authority',
               'Resolve reconsideration ownership for this episode without resuming the held scope',
               action='resolve_release_authority')
        row['release']['authority'] = 'unavailable'
    elif facts.get('human_step'):
        _block(row, facts['human_step'], 'Supply the recorded human step: ' + facts['human_step'],
               action='supply_recorded_human_step')
    elif facts.get('dependencies'):
        deps = facts['dependencies']
        _block(row, '; '.join(f"{d['task_id']} ({d['status'] or 'unknown'})" for d in deps),
               '; '.join(d['trigger'] for d in deps), action='satisfy_dependency')
    elif status == 'scheduled':
        _schedule(row, facts, now)
    elif status == 'running':
        run = facts.get('run') or {}
        if not run or run.get('status') != 'running' or run.get('ended_at') is not None:
            if facts.get('run_evidence_known'):
                row['violations'].append('running_without_current_run')
            else:
                row['evidence_state'] = 'unknown'
            _block(row, 'Current live executor/run witness',
                   'Dispatcher verifies worker loss through existing reclaim APIs; preserve pending stop custody',
                   action='repair_lifecycle_contract')
        else:
            row.update(action='execute_current_scope', instruction='Current executor continues its admitted scope')
            # A persisted running row cannot prove process/lease liveness without a fresh witness.
            row['evidence_state'] = 'unknown'
    elif status in ('ready', 'review'):
        row.update(action='review_current_scope' if status == 'review' else 'execute_current_scope',
                   instruction='Assigned profile requests normal admission; all existing holds and review gates apply')
        row['evidence_state'] = 'unknown'  # read-only database evidence is not live eligibility
    elif status == 'todo':
        row['violations'].append('dependency_transition_due')
        _block(row, 'Existing recompute_ready transition',
               'Dispatcher recomputes satisfied dependencies, preserving review phase and all execution holds',
               action='repair_lifecycle_contract')
    elif status == 'triage':
        row.update(action='decide_scope', instruction='Assigned profile specifies or reviews scope before explicit promotion')
    else:
        row['violations'].append('lifecycle_contract_missing')
        _block(row, 'Supported lifecycle transition', 'Assigned owner repairs the lifecycle contract without bypassing holds',
               action='repair_lifecycle_contract')


def valid_awaiting(value) -> bool:
    """Validate recorded producer provenance without normalizing away malformed evidence."""
    if value is None or value == {}:
        return True  # absent provenance may still have a recorded condition probe
    if not isinstance(value, dict):
        return False
    if any(value.get(k) is not None and not isinstance(value[k], str)
           for k in ('ref', 'why', 'gateway', 'board')):
        return False
    kind = value.get('kind')
    if kind == 'clock':
        return True
    field = {'card': 'ref', 'ci': 'ref', 'deploy': 'ref', 'chat': 'why', 'external': 'ref', 'resource': 'ref'}
    if not isinstance(kind, str) or kind not in field:
        return False
    named = value.get(field[kind])
    if kind == 'external' and not named:
        named = value.get('why')
    return isinstance(named, str) and bool(named.strip())


def _schedule(row: dict, facts: dict, now: int) -> None:
    wake = positive_time(facts.get('wake_at'))
    valid = valid_awaiting(facts.get('awaiting'))
    awaiting = (facts.get('awaiting') or {}) if valid else {}
    if not valid:
        row['evidence_state'] = 'unknown'
        row['violations'].append('schedule_producer_malformed')
    probe = facts.get('probe')
    if not wake:
        row['violations'].append('scheduled_wake_missing')
    elif now > wake + 600:
        row['violations'].append('scheduled_wake_overdue')
    trigger = f"Recorded clock reaches {wake}" if awaiting.get('kind') == 'clock' and wake else (
        f"Recorded {awaiting.get('kind')} producer {awaiting.get('ref') or awaiting.get('why') or ''}" if awaiting else '')
    if probe and valid:
        trigger = f'Recorded condition probe succeeds: {probe}'
    if not trigger:
        row['violations'].append('recovery_trigger_missing')
    _block(row, trigger or 'Named finite producer and recovery condition',
           trigger or 'Repair the missing wake/producer through existing scheduling APIs',
           action='repair_lifecycle_contract' if row['violations'] else 'wait_for_named_producer')


def project(facts: dict, *, now: int) -> dict:
    """Project only supplied facts. `now` affects overdue display, never identity/deadline."""
    task = facts['task']
    identity = {key: facts.get(key) for key in ('source_uid', 'stop_episode', 'containment',
                                              'administrative_owner')}
    identity['dependencies'] = [{k: dep.get(k) for k in ('task_id', 'status', 'owner', 'stop_episode', 'containment')}
                                for dep in facts.get('dependencies', [])]
    command = facts.get('pending_command')
    identity['pending_command'] = {k: command.get(k) for k in ('request_uid', 'request_sha256', 'run_uid', 'operation_kind')} if command else None
    identity['stop_cause'] = (facts.get('stop') or {}).get('cause')
    identity.update(task_id=task['id'], status=task['status'], assignee=task.get('assignee'))
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    first = positive_time(facts.get('source_at')) or positive_time(task.get('created_at'))
    deadline = positive_time(facts.get('deadline_at'))
    wake = positive_time(facts.get('wake_at'))
    if task['status'] == 'scheduled' and wake and not any(
            facts.get(k) for k in ('pending_command', 'stop', 'containment', 'human_step', 'dependencies')):
        deadline = max(deadline or 0, wake + 600)
    row = {'version': 1, 'task_id': task['id'], 'status': task['status'], 'contract_key': key,
           'evidence_state': 'unknown' if facts.get('unknown') else 'known',
           'owner': facts.get('administrative_owner') or owner(task.get('assignee')),
           'action': 'repair_lifecycle_contract', 'instruction': '', 'blocker': None,
           'release': {'authority': 'existing_lifecycle_gates', 'condition': 'Existing authenticated lifecycle policy'},
           'escalation': {'at': deadline, 'route': 'existing_decision_requested', 'state': 'pending', 'receipt_uid': None},
           'violations': [], 'source_refs': facts.get('source_refs', []),
           'dependencies': facts.get('dependencies', []), 'first_observed': first,
           'materialization': 'source_derived'}
    _status(row, facts, now)
    if task['status'] not in NONTERMINAL:
        row['violations'] = []
        row['escalation'].update(at=None, state='not_applicable')
        return row
    deps = row['dependencies']
    if deps and all(d.get('restricted') and d.get('evidence_state') == 'known' for d in deps):
        row['release']['authority'] = 'unavailable'
        if all(d.get('administrative_owner', {}).get('kind') == 'unavailable' for d in deps):
            row['violations'].append('dependency_recovery_unavailable')
    if row['owner']['kind'] == 'unavailable':
        # A stopped card whose human request is stored (the stranded-park ask) is owned by that ask.
        if not (facts.get('stop') and facts.get('human_step')):
            row['violations'].append('owner_missing')
    elif row['owner']['kind'] == 'unknown' or not row['owner'].get('refs'):
        row['evidence_state'] = 'unknown'
    if not deadline:
        row['violations'].append('escalation_deadline_missing')
        row['escalation']['state'] = 'unavailable'
        row['evidence_state'] = 'unknown'
    if row['blocker'] and deadline and now >= deadline:
        row['violations'].append('escalation_overdue')
    if facts.get('unknown'):
        row['evidence_state'] = 'unknown'
    return row
