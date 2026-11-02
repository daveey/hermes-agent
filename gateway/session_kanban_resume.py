"""[daveey] A Kanban run requeued by its owner's restart continues the lost run's conversation.

A gateway restart (every core update) cuts off each managed Kanban turn it owns; the sweep requeues
the run uncounted (``owner_restart_lost``). The next run used to start a fresh session carrying only the
bounded transcript tail from ``hermes_cli.kanban_worker_recovery``, so everything since the last checkpoint
was redone. Now, when the run immediately before this one on the card is such a restart loss, the owner
copies that run's persisted active transcript into the NEW run's own fresh session before the first turn,
the way a resumed Desktop chat replays its stored history.

Admission and ownership do not change: the new run keeps its own run id, claim, session id and
``owner_admitted`` marker; only where the conversation history comes from differs. The old session is
read, never written, resumed or leased.

Fallback (today's fresh session with the transcript tail) whenever the copy cannot be proven safe:
the previous session is missing, empty or unreadable; it lives in another home/state.db or belongs to
another profile; or its transcript already exceeds the model's compaction threshold (a fresh session
with the tail is cheaper than a resume whose first act is a lossy compaction). A previous turn that
stopped mid-tool-call with no recorded result is still resumed, but each unanswered call gets a synthetic
"interrupted by gateway restart" result instead of being replayed.
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

EVENT_KIND = 'restart_resume'
INTERRUPTED_TOOL_RESULT = (
    'Interrupted by gateway restart: this tool call may or may not have run and its result was not '
    'recorded. Re-check the current state (files, processes, remote effects) before repeating any side effect.'
)
RESUME_NOTE = (
    '\n## Resumed after gateway restart\n'
    'The previous run of this card (run {prior_run}) was cut off by a gateway restart and requeued without '
    'counting a failure. Its full conversation above is restored into this run\'s session; continue that work '
    'instead of starting over. Tool calls interrupted by the restart may or may not have taken effect: verify '
    'files, running processes and remote side effects before repeating them. Use only THIS run\'s claim '
    '(run {run_id}); the old run\'s ownership is gone. The card state below is current.\n'
)


def _json_dict(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def restart_candidate(conn: sqlite3.Connection, task_id: str, run_id: int) -> Optional[dict]:
    """The run immediately before ``run_id`` on this card when it was lost to an owner restart, with the
    session its owner admitted it to; ``None`` for every other requeue (crash, timeout, quota, block...).

    Only the immediately preceding run counts: a crash or timeout in between means the restart loss is
    not what this run is recovering from, and the ordinary path applies unchanged.
    """
    from hermes_cli.kanban_db_dispatch import _is_owner_restart_run
    prev = conn.execute('SELECT * FROM task_runs WHERE task_id=? AND id<? ORDER BY id DESC LIMIT 1',
                        (task_id, run_id)).fetchone()
    if prev is None or prev['ended_at'] is None or not _is_owner_restart_run(prev):
        return None
    candidate = {'prior_run_id': int(prev['id']), 'prior_profile': prev['profile']}
    admitted = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='owner_admitted' "
                            'ORDER BY id DESC LIMIT 1', (task_id, prev['id'])).fetchone()
    payload = _json_dict(admitted[0] if admitted else None)
    if isinstance(payload.get('session_id'), str) and payload['session_id']:
        candidate.update(session_id=payload['session_id'], state_db=payload.get('db'))
    return candidate


def _unanswered_tool_calls(history: list[dict]) -> list[dict]:
    """Calls of the LAST assistant tool-call message with no result after it, when only tool results
    follow it (an in-order synthetic result can only be appended there). Earlier orphans are left to the
    agent's own sequence repair."""
    from agent.message_sanitization import coalesce_tool_call_id, tool_result_id_variants
    for index in range(len(history) - 1, -1, -1):
        msg = history[index]
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'tool':
            continue
        if msg.get('role') != 'assistant' or not msg.get('tool_calls'):
            return []
        answered = set()
        for later in history[index + 1:]:
            for variant in tool_result_id_variants(later.get('tool_call_id') or ''):
                answered.add(variant)
        missing = []
        for call in msg['tool_calls'] if isinstance(msg['tool_calls'], list) else []:
            if not isinstance(call, dict):
                continue
            call_id = coalesce_tool_call_id(call)
            if not call_id:
                continue
            if any(v in answered for v in tool_result_id_variants(call_id)):
                continue
            name = (call.get('function') or {}).get('name') if isinstance(call.get('function'), dict) else None
            missing.append({'id': call_id, 'name': name or call.get('name') or ''})
        return missing
    return []


def plan_resume(candidate: Optional[dict], *, task, run_id: int, session_db, owner_db: Optional[str],
                threshold_tokens: Callable[[], Optional[int]]) -> Optional[dict]:
    """``None`` when this run does not follow an owner-restart loss; else a plan: ``{'resumed_session_id',
    ...}`` to copy, or ``{'fallback': reason, ...}`` for today's fresh session with the transcript tail."""
    if candidate is None:
        return None
    plan = {'prior_run_id': candidate['prior_run_id'], 'run_id': run_id}

    def fallback(reason, **extra):
        return {**plan, 'fallback': reason, **extra}

    sid = candidate.get('session_id')
    if not sid:
        return fallback('prior_session_missing')
    plan['prior_session_id'] = sid
    if candidate.get('prior_profile') != task.assignee:
        return fallback('foreign_profile')
    try:
        here = Path(owner_db).resolve() if owner_db else None
        there = Path(candidate.get('state_db') or '').resolve() if candidate.get('state_db') else None
    except (OSError, TypeError, ValueError):
        here = there = None
    if here is None or there is None or here != there:
        return fallback('foreign_home')
    try:
        with closing(sqlite3.connect(here.as_uri() + '?mode=ro', uri=True, timeout=2)) as state:
            row = state.execute('SELECT source, profile_name FROM sessions WHERE id=?', (sid,)).fetchone()
            count = state.execute('SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1', (sid,)).fetchone()[0]
    except (OSError, sqlite3.Error):
        return fallback('prior_session_unreadable')
    if row is None:
        return fallback('prior_session_missing')
    if tuple(row) != ('kanban', task.assignee):
        return fallback('foreign_profile')
    if not count:
        return fallback('prior_session_empty')
    try:
        history = session_db.get_messages_as_conversation(sid)
    except Exception:  # a corrupt/odd transcript is a fallback, never an admission failure
        logger.warning('Kanban restart resume: prior session %s unreadable', sid, exc_info=True)
        return fallback('prior_session_unreadable')
    if not history:
        return fallback('prior_session_empty')
    from agent.model_metadata import estimate_messages_tokens_rough
    from agent.usage_anchor import persisted_anchor_tokens
    tokens = persisted_anchor_tokens(session_db, sid, history) or estimate_messages_tokens_rough(history)
    try:
        threshold = threshold_tokens()
    except Exception:
        logger.warning('Kanban restart resume: compaction threshold unavailable', exc_info=True)
        threshold = None
    plan.update(messages=len(history), tokens=int(tokens))
    if not threshold:
        return fallback('threshold_unknown', **{k: plan[k] for k in ('messages', 'tokens')})
    plan['threshold'] = int(threshold)
    if tokens >= threshold:
        return fallback('over_compaction_threshold')
    return {**plan, 'resumed_session_id': sid, 'interrupted_calls': _unanswered_tool_calls(history)}


def compaction_threshold_tokens(policy) -> Optional[int]:
    """The built-in compressor's trigger for this run's model, from the same config parser and compressor
    the worker's agent uses. ``None`` (fall back) when it cannot be resolved."""
    from types import SimpleNamespace
    from agent.agent_init import _parse_compression_config
    from agent.context_compressor import ContextCompressor
    config = policy.config() if callable(getattr(policy, 'config', None)) else {}
    model = getattr(policy, 'model', None)
    if not model:
        return None
    probe = SimpleNamespace(model=model, provider=getattr(policy, 'provider', None) or '', api_mode='',
                            base_url=getattr(policy, 'base_url', None) or '')
    settings = _parse_compression_config(probe, config)
    model_cfg = config.get('model') if isinstance(config, dict) else None
    ctx = model_cfg.get('context_length') if isinstance(model_cfg, dict) else None
    from hermes_cli.config import get_compatible_custom_providers
    custom = get_compatible_custom_providers(config) if isinstance(config, dict) else None
    compressor = ContextCompressor(
        model=model, threshold_percent=settings.threshold, quiet_mode=True, base_url=probe.base_url,
        config_context_length=ctx if isinstance(ctx, int) and ctx > 0 else None, provider=probe.provider,
        model_thresholds=settings.model_thresholds, threshold_tokens_cap=settings.threshold_tokens,
        custom_providers=custom)
    return int(compressor.threshold_tokens) or None


def seed_resumed_session(session_db, session_id: str, plan: dict) -> int:
    """Copy the prior session's active rows into the fresh ``session_id`` (one write transaction) and
    append a synthetic result for each unanswered trailing tool call. Returns the rows written; 0 when
    the target already has messages (a retried admission) or there is nothing to copy."""
    source = plan['resumed_session_id']
    calls = list(plan.get('interrupted_calls') or ())
    synthetic: list[dict[str, Any]] = []
    if calls:
        from agent.message_metadata import TOOL_CALL_UID, tool_call_uid_from_history
        history = session_db.get_messages_as_conversation(source)
        for call in calls:
            msg = {'role': 'tool', 'tool_call_id': call['id'], 'content': INTERRUPTED_TOOL_RESULT}
            if call.get('name'):
                msg['tool_name'] = call['name']
            uid = tool_call_uid_from_history([*history, msg], len(history), {})
            if uid:
                msg[TOOL_CALL_UID] = uid
            synthetic.append(msg)

    def write(conn):
        conn.row_factory = sqlite3.Row
        if conn.execute('SELECT 1 FROM messages WHERE session_id=? LIMIT 1', (session_id,)).fetchone():
            return 0
        ids, tools = session_db._tail_rows_after_watermark(
            conn, 'SELECT id, tool_calls FROM messages WHERE session_id=? AND active=1 ORDER BY id', [source])
        if not ids:
            return 0
        session_db._clone_message_rows(conn, ids, session_id=session_id)
        added = 0
        if synthetic:
            added, _ = session_db._insert_message_rows(conn, session_id, synthetic)
        conn.execute('UPDATE sessions SET message_count=?, tool_call_count=? WHERE id=?',
                     (len(ids) + added, tools, session_id))
        return len(ids) + added

    return int(session_db._execute_write(write))


def record_resume_event(conn, task_id: str, run_id: int, plan: dict, copied: Optional[int]) -> None:
    """One ``restart_resume`` event per admitted attempt that follows an owner-restart loss."""
    from hermes_cli import kanban_db as kb
    payload = {k: plan[k] for k in ('prior_run_id', 'prior_session_id', 'messages', 'tokens', 'threshold',
                                    'fallback') if plan.get(k) is not None}
    if plan.get('resumed_session_id'):
        if copied:
            payload.update(resumed_session_id=plan['resumed_session_id'], copied_messages=copied,
                           interrupted_calls=len(plan.get('interrupted_calls') or ()))
        else:
            payload['fallback'] = 'seed_failed'
    kb._append_event(conn, task_id, EVENT_KIND, payload, run_id=run_id)


def resume_context(base_context: str, plan: dict) -> str:
    return RESUME_NOTE.format(prior_run=plan['prior_run_id'], run_id=plan['run_id']) + base_context


def worker_prompt_context(context: dict, history) -> str:
    """The worker's card context: the resume variant only when the copied history really arrived
    (a failed or skipped seed leaves the session empty, and the transcript-tail context applies)."""
    if history and context.get('resume_context'):
        return context['resume_context']
    return context['context']
