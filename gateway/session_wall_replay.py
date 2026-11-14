"""[daveey] One redelivery of an automated wake that a held quota wall refused before it ran.

A Kanban notifier (or other producer) wake is committed to the session FIFO (``admit_route_wake``), and the
producer takes that receipt as final: it never resends. When the chat is pinned to a route whose subscription
wall *holds* (an explicit pin, or a wall under ``model_routing_walls.WALL_REROUTE_MIN_SECONDS``), the turn is
refused before the agent is built (``SubscriptionQuotaUnavailable``) and the reply tells a person to "resend
after the reset". Nobody resends an automated notice, so it was lost until some other event woke the chat
(hub chat local-be7f1ded…, 2026-11-14: three notifier wakes refused, none redelivered).

This module is that resend, bounded so it cannot loop:

- only an unattended route wake (principal ``automation:<route>``, a ``kanban-wake`` or ``background-wake``
  identity) refused pre-build by a quota wall with a known reset (``failure_resets_at``, ``api_calls == 0``)
  plans one; a person's message keeps today's held reply and nothing is scheduled;
- the plan is durable (``state_meta`` key :data:`PREFIX` + admission id) and armed in this process; a gateway
  restart re-arms it at boot (:func:`arm_pending`), so a multi-hour wall survives the hub's frequent restarts;
- at ``max(resets_at + SLACK_S, now + MIN_DELAY_S)`` the wake is re-admitted through the same producer entry
  (``admit_route_wake``) under the deterministic identity ``[producer, route, "wall-replay:<tail>"]``: a second
  arm or a race re-admits the same row (an exact replay), never a second turn;
- a replay that is refused again plans nothing (its identity says it is one) and the reply says so;
- a plan cannot outlive its use: one whose time passed more than :data:`STALE_S` ago (a long outage) is
  dropped at boot, not delivered, and the admission retry budget (:data:`RETRIES_MAX`) is persisted in the
  plan, so a route that never comes back (a platform removed from config) or an admission that keeps
  raising is dropped once the budget is spent, however often the gateway restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = 'automation_wall_replay:'
REPLAY_TAG = 'wall-replay:'
PRODUCERS = frozenset({'kanban-wake', 'background-wake'})
# The replay runs this long after the recorded reset (a quota window rarely reopens on the exact second).
SLACK_S = 60.0
# Never sooner than this after the refusal or a boot (adapters connect after the authority is built).
MIN_DELAY_S = 60.0
# A wall further out than this is not worth a timer: the notice would be stale by then.
MAX_DELAY_S = 7 * 86400.0
# A replay that cannot be admitted yet (no adapter, a draining gateway) looks again this much later, this often.
RETRY_S = 120.0
# The budget spans restarts: the tries spent are stored in the plan (``tries``), not counted per process.
RETRIES_MAX = 10
# A plan whose time passed this long ago (the gateway was down through it) is dropped, not delivered late.
STALE_S = 86400.0
# Descriptor keys a route wake stores (``snapshot_*automation``) and ``admit_route_wake`` accepts back.
_CARRIED = ('notification_category', 'display_kind', 'display_text', 'kanban_cards', 'kanban_event_keys')
_TRANSIENT = frozenset({'runtime_draining', 'busy', 'database_busy', 'stale_generation'})


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _identity(row: dict) -> list | None:
    """``[producer, route, tail]`` of an unattended route wake, else None."""
    principal = str(row.get('principal_id') or '')
    try:
        parts = json.loads(row.get('request_id') or '')
    except (TypeError, ValueError):
        return None
    if (not isinstance(parts, list) or len(parts) != 3 or not all(isinstance(p, str) for p in parts)
            or parts[0] not in PRODUCERS or principal != 'automation:' + parts[1]):
        return None
    return parts


def is_replay(row: dict) -> bool:
    parts = _identity(row)
    return parts is not None and parts[2].startswith(REPLAY_TAG)


def _descriptor(payload: dict) -> dict:
    return payload.get('local_automation_v1') or (payload.get('native_text_v1') or {}).get('automation') or {}


def wall_resets_at(result: Any) -> float | None:
    """The reset of a pre-build quota-wall refusal (``_resolution_failure_result``), else None."""
    if not isinstance(result, dict) or not result.get('failed') or result.get('api_calls'):
        return None
    if result.get('failure_reason') not in ('route_refused', 'auth_permanent'):
        return None
    return _number(result.get('failure_resets_at'))


def plan_due(row: dict, result: Any, now: float | None = None) -> float | None:
    """When to redeliver this refused automation wake, or None (a person's turn, a replay, another failure)."""
    reset = wall_resets_at(result)
    if reset is None or _identity(row) is None or is_replay(row):
        return None
    now = time.time() if now is None else now
    due = max(reset + SLACK_S, now + MIN_DELAY_S)
    return due if due - now <= MAX_DELAY_S else None


def _when(ts: float) -> str:
    from agent.model_routing_walls import reset_pt
    return reset_pt(ts)


def scheduled_line(due: float) -> str:
    return (f"This was an automated notice, not a message you sent: Hermes will deliver it once more at "
            f"{_when(due)}, after the reset.")


GAVE_UP_LINE = ("This was the one automatic redelivery of an automated notice, refused again by the same wall; "
                "nothing more is scheduled.")


def _amend(result: dict, response: Any, line: str) -> Any:
    """Append ``line`` to the reply (and the error card's own sentence, which is the same text)."""
    old = result.get('final_response')
    if isinstance(old, str) and old.strip():
        result['final_response'] = f"{old}\n\n{line}"
        surface = result.get('error_surface')
        if isinstance(surface, dict) and surface.get('message') == old.strip():
            surface['message'] = result['final_response'].strip()
    if isinstance(response, str) and response.strip():
        return f"{response}\n\n{line}"
    return response


def _intent(row: dict, source: Any, due: float) -> dict:
    parts = _identity(row) or ['', '', '']
    payload = row.get('payload') or {}
    descriptor = _descriptor(payload)
    to_dict = getattr(source, 'to_dict', None)
    return {'admission_id': row['admission_id'], 'session_id': row.get('target_session_id'),
            'producer': parts[0], 'route': parts[1], 'tail': parts[2], 'due': due, 'failed_at': time.time(),
            'text': payload.get('text') if isinstance(payload.get('text'), str) else '',
            'metadata': {k: descriptor[k] for k in _CARRIED if k in descriptor},
            'source': to_dict() if callable(to_dict) else None}


def _write(db, key: str, value: str | None) -> None:
    def write(conn):
        if value is None:
            conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
        else:
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET '
                         'value=excluded.value', (key, value))
    db._execute_write(write)


def _load(db) -> list[dict]:
    with db._read_ctx() as conn:
        rows = conn.execute('SELECT value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall()
    intents = []
    for (value,) in rows:
        try:
            intent = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(intent, dict) and intent.get('admission_id'):
            intents.append(intent)
    return intents


async def after_turn(authority, row: dict, source: Any, result: Any, response: Any) -> Any:
    """Plan (and arm) the redelivery of a refused automation wake; returns the reply to send, amended with
    what happens next. Every other turn passes through unchanged. Never raises."""
    try:
        if wall_resets_at(result) is None:
            return response
        if is_replay(row):
            logger.warning('wall-replay: the redelivery %s was refused again by the wall; giving up',
                           row.get('admission_id'))
            return _amend(result, response, GAVE_UP_LINE)
        due = plan_due(row, result)
        if due is None:
            return response
        intent = _intent(row, source, due)
        if intent['source'] is None:
            return response
        await asyncio.to_thread(_write, authority.db, PREFIX + intent['admission_id'], json.dumps(intent))
        _arm(authority, intent)
        logger.info('wall-replay: automated wake %s on %s refused by a held quota wall; one redelivery at %s',
                    intent['admission_id'], intent['route'], _when(due))
        return _amend(result, response, scheduled_line(due))
    except Exception:  # health: allow BLE001 -- planning a resend must never fail the refused turn's settlement
        logger.warning('wall-replay: could not plan the redelivery of %s', row.get('admission_id'), exc_info=True)
        return response


def _armed(authority) -> dict:
    armed = getattr(authority, '_wall_replay_timers', None)
    if armed is None:
        armed = authority._wall_replay_timers = {}
    return armed


def _arm(authority, intent: dict, delay: float | None = None, tries: int = 0) -> None:
    loop = asyncio.get_running_loop()
    admission_id = intent['admission_id']
    armed = _armed(authority)
    old = armed.pop(admission_id, None)
    if old is not None:
        old.cancel()
    if delay is None:
        delay = max(float(intent['due']) - time.time(), MIN_DELAY_S)

    def fire():
        armed.pop(admission_id, None)
        task = loop.create_task(fire_replay(authority, admission_id, tries))
        pending = getattr(authority, '_wall_replay_tasks', None)
        if pending is None:
            pending = authority._wall_replay_tasks = set()
        pending.add(task)
        task.add_done_callback(pending.discard)
    armed[admission_id] = loop.call_later(max(delay, 0.0), fire)


def _tries(intent: dict) -> int:
    tries = _number(intent.get('tries'))
    return int(tries) if tries is not None and tries > 0 else 0


def _stale(intent: dict, now: float) -> bool:
    due = _number(intent.get('due'))
    return due is None or now - due > STALE_S


def _expired(intent: dict, now: float) -> str | None:
    """Why a stored plan must be dropped rather than armed or delivered, else None."""
    if _stale(intent, now):
        return 'stale'
    if _tries(intent) >= RETRIES_MAX:
        return 'out of tries'
    return None


async def arm_pending(authority) -> int:
    """Boot: re-arm every planned redelivery this authority's store holds (at least ``MIN_DELAY_S`` out, so
    the adapters are connected first), dropping the expired ones (:func:`_expired`). Returns how many were
    armed. Never raises."""
    try:
        intents = await asyncio.to_thread(_load, authority.db)
    except Exception:  # health: allow BLE001 -- an unreadable store loses only the redeliveries
        logger.warning('wall-replay: could not read the planned redeliveries', exc_info=True)
        return 0
    armed = 0
    now = time.time()
    for intent in intents:
        why = _expired(intent, now)
        if why is not None:
            logger.warning('wall-replay: dropped the planned redelivery of %s at boot (%s)',
                           intent['admission_id'], why)
            try:
                await asyncio.to_thread(_write, authority.db, PREFIX + intent['admission_id'], None)
            except Exception:  # health: allow BLE001 -- not armed either way; the next boot drops it again
                logger.debug('wall-replay: could not drop %s', intent['admission_id'], exc_info=True)
            continue
        _arm(authority, intent, tries=_tries(intent))
        armed += 1
    if armed:
        logger.info('wall-replay: re-armed %d planned redelivery(ies) at boot', armed)
    return armed


def _serving(authority) -> bool:
    if getattr(authority, 'retiring', False):
        return False
    from gateway.session_authorities import serving_authorities
    return any(a is authority for a in serving_authorities(authority.runner))


async def fire_replay(authority, admission_id: str, tries: int = 0) -> str:
    """Re-admit the planned wake once. Returns ``admitted``/``dropped``/``retry``/``gone``."""
    key = PREFIX + admission_id
    try:
        raw = await asyncio.to_thread(lambda: _get(authority.db, key))
    except Exception:  # health: allow BLE001 -- unreadable now: look again later
        logger.warning('wall-replay: could not read %s', key, exc_info=True)
        return await _retry(authority, {'admission_id': admission_id, 'due': time.time()}, tries)
    if raw is None:
        return 'gone'
    intent = json.loads(raw)
    if not _serving(authority):
        return 'gone'  # a retired authority's store is re-armed by the process that serves it
    tries = max(tries, _tries(intent))
    if _stale(intent, time.time()):
        logger.warning('wall-replay: the redelivery of %s is stale; dropped', admission_id)
        await asyncio.to_thread(_write, authority.db, key, None)
        return 'dropped'
    try:
        verdict = await _admit(authority, intent)
    except Exception:  # health: allow BLE001 -- an unexpected admission error retries within the persisted budget
        logger.warning('wall-replay: admitting the redelivery of %s failed', admission_id, exc_info=True)
        verdict = 'retry'
    if verdict == 'retry':
        return await _retry(authority, intent, tries, persist=True)
    await asyncio.to_thread(_write, authority.db, key, None)
    return verdict


def _get(db, key: str) -> str | None:
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return None if row is None else row[0]


async def _retry(authority, intent: dict, tries: int, persist: bool = False) -> str:
    """Look again in ``RETRY_S``, or drop the plan once ``RETRIES_MAX`` tries are spent. ``persist`` stores
    the spent tries in the plan (only for a plan read whole from the store), so a restart keeps counting."""
    if tries >= RETRIES_MAX:
        logger.warning('wall-replay: gave up admitting the redelivery of %s after %d tries',
                       intent['admission_id'], tries)
        try:
            await asyncio.to_thread(_write, authority.db, PREFIX + intent['admission_id'], None)
        except Exception:  # health: allow BLE001 -- the next boot re-arms it under its stored tries, still bounded
            logger.debug('wall-replay: could not drop %s', intent['admission_id'], exc_info=True)
        return 'dropped'
    if persist:
        try:
            await asyncio.to_thread(_write, authority.db, PREFIX + intent['admission_id'],
                                    json.dumps({**intent, 'tries': tries + 1}))
        except Exception:  # health: allow BLE001 -- this process still counts; a restart may grant a few more
            logger.debug('wall-replay: could not record the tries of %s', intent['admission_id'], exc_info=True)
    _arm(authority, intent, RETRY_S, tries + 1)
    return 'retry'


async def _admit(authority, intent: dict) -> str:
    from gateway.run import _async_profile_runtime_scope
    from gateway.session import SessionSource
    from gateway.session_automation import admit_route_wake
    from hermes_state_runtime import RuntimeStoreError
    runner = authority.runner
    try:
        source = SessionSource.from_dict(intent['source'])
    except (KeyError, TypeError, ValueError):
        logger.warning('wall-replay: %s has no usable route source; dropped', intent.get('admission_id'))
        return 'dropped'
    adapter = runner._adapter_for_source(source)
    if adapter is None:
        return 'retry'  # not connected yet (boot), or a platform that comes back
    try:
        async with _async_profile_runtime_scope(runner._resolve_profile_home_for_source(source)):
            admitted = await admit_route_wake(runner, adapter, source, intent.get('text') or '', intent['producer'],
                                              dict(intent.get('metadata') or {}), replay_of=intent['tail'])
    except RuntimeStoreError as exc:
        if exc.reason in _TRANSIENT:
            return 'retry'
        logger.warning('wall-replay: redelivery of %s refused (%s); dropped', intent['admission_id'], exc.reason)
        return 'dropped'
    if not admitted:
        logger.warning('wall-replay: the route of %s (%s) no longer takes wakes; dropped',
                       intent['admission_id'], intent['route'])
        return 'dropped'
    logger.info('wall-replay: redelivered automated wake %s on %s', intent['admission_id'], intent['route'])
    return 'admitted'
