"""HOME atomic application. Consumed proof is never general execution clearance.

Only the internal promotion body receives the validated writer-local admission.
The private APPLIED receipt is written last; any later hold aborts ALL effects.
Network authentication must precede apply(), never run while its writer is held.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert
from hermes_cli import kanban_release_replica as replica, kanban_scope_renewal as renewal


def _scope(conn, proof):
    body = proof['intent']
    evidence = renewal.scope_evidence(conn, body['task_id'], body['episode'])
    covered = cert.coverage(proof)
    if not evidence or frozenset(renewal.key(e) for e in evidence) != covered:
        raise ValueError('HOME current scope evidence differs from winning consumption')
    return covered


def _comments(conn, proof):
    tid = proof['intent']['task_id']
    rows = [dict(r) for r in conn.execute('SELECT * FROM task_comments WHERE task_id=? ORDER BY uid', (tid,))]
    if any(r.get('deleted_at') is not None for r in rows):
        raise ValueError('HOME acknowledged comment was deleted')
    expected = proof['consume']['seen_comments']
    fields = ('uid', 'task_id', 'author', 'body', 'created_at', 'origin_gateway')
    if cert.encode([{k:r.get(k) for k in fields} for r in rows]) != cert.encode([{k:r[k] for k in fields} for r in expected]):
        raise ValueError('HOME comments differ from complete acknowledged inventory')
    # Exact complete inventory acknowledges local IDs, never a foreign numeric ID.
    return max((r['id'] for r in rows), default=None)


def _validate(conn, proof, delivery_uid):
    from hermes_cli import kanban_db as kb, kanban_spend_decision as decision
    from hermes_cli.kanban_containment_guard import pending as contained
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_goal_recovery import current_park
    from hermes_cli.kanban_resume_requester import open_recovery_card
    from hermes_cli.kanban_stop_ended import stopped_at
    from hermes_cli import kanban_release as release, kanban_release_capability as capability
    body, tid = proof['intent'], proof['intent']['task_id']
    local = conn.execute('SELECT board_id,gateway_id,mode FROM kanban_sync_state LIMIT 1').fetchone()
    if local is None or tuple(local) != (body['board'], proof['renewal']['origin_gateway'], 'primary'):
        raise ValueError('HOME application is not the exact primary')
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    if (task is None or task['created_on'] != local[1] or task['status'] != 'triage'
            or task['current_run_id'] is not None or task['claim_lock'] or task['lease_holder']
            or task['human_request'] or task['block_kind'] in HUMAN_BLOCK_KINDS):
        raise ValueError('HOME task has a live claim/run/lease/human hold or changed placement')
    if conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (tid,)).fetchone():
        raise ValueError('HOME task still has a nonterminal run')
    if contained(conn, tid) or stopped_at(conn, tid) is not None or current_park(conn, tid) or open_recovery_card(conn, tid):
        raise ValueError('HOME independent containment/admin/recovery hold remains')
    from hermes_cli.kanban_refusal_receipt import restricted
    if restricted(conn, tid):
        raise ValueError('HOME provider safety restriction remains')
    links = [link for link in _chat_links(conn, tid) if link.get('kind', 'created') == 'created']
    actor = body['actor']
    if (len(links) != 1 or not links[0].get('uid')
            or (links[0]['chat_gateway'], links[0].get('chat_profile') or 'default', links[0]['chat_session'])
            != (actor['gateway'], actor['profile'], actor['session'])
            or actor['session'] in renewal._creator_sessions(conn, tid)):
        raise ValueError('HOME requires the sole unreleased original created owner, never a task run')
    stop = decision.strict_episode(conn, tid)
    if stop is None or stop['kind'] != renewal.STOP or renewal.key(stop) != body['episode']:
        raise ValueError('HOME current episode changed')
    try:
        selected = capability.selected(conn, tid, body['capability_episodes'])
    except release._Refusal as exc:
        raise ValueError(str(exc)) from exc
    covered = _scope(conn, proof)
    app._delivery(conn, proof, delivery_uid)
    if not cert.event_matches(app._event(conn, proof['renewal']['uid']), proof):
        raise ValueError('HOME winning canonical renewal is missing')
    if any(st not in ('done', 'archived') for _, st in kb.parent_states(conn, tid)):
        raise ValueError('HOME dependencies remain unsatisfied; no edge is removed')
    seen = _comments(conn, proof)
    anchor = capability.anchor(stop, selected)
    people, missing = release._human_after(conn, tid, anchor, seen)
    if people or missing:
        raise ValueError('HOME human instruction has not been acknowledged locally')
    return covered, seen


@dataclass(frozen=True, init=False, slots=True)
class _Admission:
    """Construct only by revalidating all actual live gates inside this writer."""
    conn: object
    invocation: object
    task: str
    proof_bytes: str
    delivery: str
    covered: frozenset
    seen: int | None

    def __init__(self, conn, proof, delivery):
        if app._PERMIT.get() is not conn or app._INVOCATION.get() is None or not conn.in_transaction:
            raise ValueError('HOME admission requires its unique application writer')
        stored = replica.verified_consumption(conn, proof['consume']['client_op_id'])
        if stored is None or cert.encode(stored) != cert.encode(proof):
            raise ValueError('HOME admission requires exact private consumption')
        covered, seen = _validate(conn, stored, delivery)
        values = dict(conn=conn, invocation=app._INVOCATION.get(), task=stored['intent']['task_id'],
                      proof_bytes=cert.encode(stored), delivery=delivery, covered=covered, seen=seen)
        for key, value in values.items():
            object.__setattr__(self, key, value)


def _admission_refusal(conn, tid, admission):
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_refusal_receipt import restricted
    from hermes_cli.kanban_stop_ended import stopped_at
    if (type(admission) is not _Admission or admission.conn is not conn or admission.task != tid
            or admission.invocation is not app._INVOCATION.get() or app._PERMIT.get() is not conn
            or not conn.in_transaction):
        return 'HOME application admission is not bound to this writer/connection/task'
    proof = cert.parse(admission.proof_bytes)
    if _scope(conn, proof) != admission.covered:
        return 'HOME scope evidence changed during application'
    if restricted(conn, tid) or stopped_at(conn, tid) is not None:
        return 'HOME provider safety or administrative restriction remains'
    if decision._uncovered_since(conn, tid, renewal.covered(conn, tid) | admission.covered) is not None:
        return 'HOME independent execution restriction remains'
    return None


def _budget(conn, body, seen):
    from hermes_cli import kanban_release as release, kanban_spend_budget as budget
    actor = body['actor']
    state = SimpleNamespace(tid=body['task_id'], writer=('agent', actor['session'], 'native-owner'), runs=body['runs'])
    runs, cache, _spent, text = release._budget_plan(conn, state)
    if runs is not None:
        kept = budget.carried(state.prior).get('cache_read_writer_kind', 'unknown') if state.keeps_limit else 'agent'
        prior_runs = budget.runs_attribution(state.prior) or 'unknown'
        budget.set_override_in_txn(conn, state.tid, runs=runs, cache_read_tokens=cache,
                                   by=f"{actor['gateway']}/{actor['profile']}/{actor['session']}",
                                   reason='hermes kanban release: authenticated original-owner intent ' + body['intent_uid'],
                                   seen=seen, board=body['board'], writer_kind='agent', cache_read_writer_kind=kept,
                                   runs_writer_kind=prior_runs if budget.override_set(state.prior) and prior_runs != 'agent' else None)
    return release.mandate_text(body['runs'], body['note'], agent=True, budget=text)


def _private_veto(conn, tid):
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason
    from hermes_cli.kanban_actionability_private import PRIVATE
    kinds = tuple(PRIVATE)
    if conn.execute(f"SELECT 1 FROM task_events WHERE task_id=? AND kind IN ({','.join('?' for _ in kinds)}) AND uid IS NULL",
                    (tid, *kinds)).fetchone():
        raise ValueError('HOME private restriction history is unattributable')
    task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    reader = Reader(conn)
    if not reader.events_complete or (why := _private_reason(conn, task, reader)):
        raise ValueError('HOME independent private hold remains: ' + str(why if reader.events_complete else 'incomplete history'))


def apply(conn, proof, *, delivery_uid):
    """Compose real effects atomically after authenticated private transport.

    Historical exact result resolves before mutable gates. Queue custody remains
    frozen at consumed_pending_apply; the APPLIED sidecar owns completion, not a
    transport-stage edit that would revoke its own immutable evidence.
    """
    body = proof['intent']
    prior = app.verified_application(conn, body['intent_uid'])
    if prior is not None:
        if prior['delivery_uid'] != delivery_uid or cert.encode(prior['consumption']) != cert.encode(proof):
            raise ValueError('HOME historical application replay differs')
        return prior
    app.install(conn)
    with app._writer(conn):
        prior = app.verified_application(conn, body['intent_uid'])
        if prior is not None:
            if prior['delivery_uid'] != delivery_uid or cert.encode(prior['consumption']) != cert.encode(proof):
                raise ValueError('HOME concurrent historical replay differs')
            return prior
        _compose(conn, proof, delivery_uid)
    return app.verified_application(conn, body['intent_uid'])


def _compose(conn, proof, delivery_uid):
    """Actual assembly body, requiring the caller's unique application writer.

    The proposal producer executes this unchanged body under a rollback savepoint;
    it cannot retain its provisional placement, APPLIED receipt or outbox effects.
    """
    from hermes_cli import kanban_db as kb, kanban_db_promote as promote, kanban_spend_budget as budget
    body = proof['intent']
    admission = _Admission(conn, proof, delivery_uid)
    tid, actor = body['task_id'], body['actor']
    before = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
    mandate = _budget(conn, body, admission.seen)
    base = dict(version=1, owner_release_intent_uid=body['intent_uid'], consume_uid=proof['consume']['client_op_id'],
                actor_kind='agent', actor_gateway=actor['gateway'], actor_profile=actor['profile'],
                actor_session=actor['session'], authority_kind='chat', runs=body['runs'], note=body['note'])
    for episode in [*body['capability_episodes'], body['episode']]:
        kb._append_event(conn, tid, 'stop_released', {**base, 'episode': episode})
    ok, why = promote._promote_in_txn(conn, tid, actor=f"{actor['gateway']}/{actor['profile']}/{actor['session']}",
                                    reason=body['note'], expected_status='triage', admission=admission)
    if not ok:
        raise ValueError('HOME promotion refused: ' + str(why))
    effects = [r[0] for r in conn.execute("SELECT uid FROM task_events WHERE task_id=? AND id>? AND kind IN "
               "('spend_budget_set','stop_released','promoted_manual') ORDER BY id", (tid, before))]
    result = dict(version=1, intent_uid=body['intent_uid'], consume_uid=proof['consume']['client_op_id'],
                  delivery_uid=delivery_uid, outcome='released', mandate=mandate, effect_uids=effects)
    kb._append_event(conn, tid, 'owner_release_applied', result)
    result_uid = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='owner_release_applied' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0]
    app._record(conn, consume_uid=proof['consume']['client_op_id'], delivery_uid=delivery_uid, result_uid=result_uid)
    _private_veto(conn, tid)  # Real APPLIED readers, not a hypothetical forged release.
    if budget.over(budget.spend(conn, tid), budget.card_limits(conn, tid, budget.limits())):
        raise ValueError('HOME cumulative spend remains over budget')
