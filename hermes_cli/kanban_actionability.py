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


RELEASE_AUTHORITY = 'kanban_release'
RELEASE_VERB = ("A human operator, the card's owning chat or a delegate chat runs "
                "`hermes kanban release {task}` on the card's home gateway "
                "(goal-level mandate, renew, budget, promote). A condition an earlier run recorded is a note for "
                "that decision, not a gate")


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
    recorded = (release['condition'] if isinstance(release, dict) and isinstance(release.get('condition'), str)
                and release['condition'].strip() else None)
    offer = facts.get('release_offer')
    if (facts['stop'].get('cause') == 'safety_refusal' or facts.get('containment')
            or not isinstance(offer, dict) or offer.get('task') != row['task_id']):
        # Only a stop `hermes kanban release` would act on names it (`release_offer` is kanban_release.offer():
        # held in triage/scheduled on a pending stop, not safety-restricted). Safety refusals, containment holds,
        # receipt-only and non-held rows keep 'unavailable': only their own authority lifts them.
        if recorded:
            row['release']['condition'] = recorded
        row['release']['authority'] = 'unavailable'  # recorded conditions never grant release
        return
    # A scope or spend stop has a release: name it. Reporting 'unavailable' under the run's own prose made chats
    # treat a worker-invented prerequisite ("policy-owner permission ... no principal can issue it") as binding,
    # and three Cogamer Tools cards looped on it for a day (2026-11-24). The recorded text never grants release.
    verb = RELEASE_VERB.format(task=row['task_id'])
    row['release'] = {'authority': RELEASE_AUTHORITY,
                      'condition': verb + (f'. Recorded note: {recorded}' if recorded else '')}


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
    elif facts.get('lane_hold') and not facts.get('stop'):
        row['owner'] = {'kind': 'assignee', 'refs': ['lane-planner']}
        _block(row, 'lane capacity', 'lane admission', action='wait_for_lane_admission')
        row['release']['authority'] = 'unavailable'
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


def _name_reason(row: dict, facts: dict) -> None:
    """A park's recorded `awaiting.why` is what it waits FOR; the clock/probe/producer trigger is only the next
    check, so a timer never stands in for the prerequisite. Read-time only: a frozen creation intent keeps the
    trigger-only wording, or every stored intent would stop matching its recomputation."""
    blocker = row.get('blocker')
    if row['status'] != 'scheduled' or not blocker or row['action'] not in (
            'wait_for_named_producer', 'satisfy_dependency', 'repair_lifecycle_contract'):
        return
    awaiting = facts.get('awaiting')
    if not isinstance(awaiting, dict) or not valid_awaiting(awaiting) or awaiting.get('inferred'):
        return
    from hermes_cli.kanban_wait_info import PREREQUISITE_MAX
    why = ' '.join(str(awaiting.get('why') or '').split())[:PREREQUISITE_MAX]
    trigger = blocker['trigger']
    if not why or trigger.startswith('Repair the missing'):
        return  # nothing recorded, or no trigger to pair it with
    wake = positive_time(facts.get('wake_at'))
    backstop = (f'; backstop: recorded clock reaches {wake}'
                if wake and awaiting.get('kind') == 'clock' and 'clock reaches' not in trigger else '')
    if row['action'] == 'satisfy_dependency':
        blocker['prerequisite'] = f"{why}; parents: {blocker['prerequisite']}"
    else:
        blocker['prerequisite'] = why
    row['instruction'] = f'Waiting on: {why}. Next check: {trigger}{backstop}'


def project(facts: dict, *, now: int, frozen: bool = False) -> dict:
    """Project only supplied facts. `now` affects overdue display, never identity/deadline. `frozen` is the
    byte-stable form a creation intent stores (no recorded-reason wording)."""
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
    invalid_creation = facts.get('invalid_creation_intent')
    if task['status'] == 'scheduled' and wake and not invalid_creation and not any(
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
    if not frozen:
        _name_reason(row, facts)
    if task['status'] not in NONTERMINAL:
        row['violations'] = []
        row['escalation'].update(at=None, state='not_applicable')
        return row
    deps = row['dependencies']
    if deps and all(d.get('restricted') and d.get('evidence_state') == 'known' for d in deps):
        if row['release']['authority'] == RELEASE_AUTHORITY:
            row['release']['condition'] = ('Every parent is restricted; a release would only record the decision and '
                                           'wait on them. ' + row['release']['condition'])
        row['release']['authority'] = 'unavailable'
        if all(d.get('administrative_owner', {}).get('kind') == 'unavailable' for d in deps):
            row['violations'].append('dependency_recovery_unavailable')
    if row['owner']['kind'] == 'unavailable':
        # A stopped card still parked with its human request stored (the stranded-park ask) is owned by that
        # ask. In any other status the stored request is not what the card waits on.
        if not (facts.get('stop') and facts.get('human_step') and task['status'] in ('triage', 'blocked')):
            row['violations'].append('owner_missing')
    elif row['owner']['kind'] == 'unknown' or not row['owner'].get('refs'):
        row['evidence_state'] = 'unknown'
    if invalid_creation:
        row['violations'].append('creation_intent_invalid')
        row['evidence_state'] = 'unknown'
    if not deadline and facts.get('private_hold'):
        # Record path refuses this card (PrivateHold): name that blocker instead of a missing deadline.
        # Action, blocker and release policy stay source-derived.
        row['violations'].append('private_hold')
        row['escalation']['state'] = 'unavailable'
        row['instruction'] = f"{row.get('instruction') or ''} Private hold: {facts['private_hold']}".strip()
        row['evidence_state'] = 'unknown'
    elif (not deadline and not invalid_creation and not row['blocker']
          and row['owner']['kind'] == 'assignee' and row['owner'].get('refs')
          and ((task['status'] in ('ready', 'running') and row['action'] == 'execute_current_scope')
               or (task['status'] == 'review' and row['action'] == 'review_current_scope'
                   and facts.get('review_dispatch') is True))):
        # A runnable card (queued for admission, a live executor, or queued for a reviewer the board dispatches)
        # is not an administrative wait: it needs no deadline. `review_dispatch` defaults to not established, and a
        # review row then keeps its missing deadline. UNKNOWN eligibility and every hold above still show, and an
        # exact confirmed contract deadline still composes onto it (kanban_actionability_confirmed).
        row['escalation']['state'] = 'not_required'
    elif not deadline:
        row['violations'].append('escalation_deadline_missing')
        row['escalation']['state'] = 'unavailable'
        row['evidence_state'] = 'unknown'
    if row['blocker'] and deadline and now >= deadline:
        row['violations'].append('escalation_overdue')
    if facts.get('unknown'):
        row['evidence_state'] = 'unknown'
    return row
