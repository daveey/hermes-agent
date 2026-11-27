"""Host/profile/role FD incidents: frozen delivery under a separate, nonblocking file lock."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
HOUR = 3600


def _profile():
    from hermes_cli.profiles import current_profile_name
    return current_profile_name(default='default')


def _path(sample, probe):
    identity = ':'.join((sample['host'], sample.get('profile') or _profile(), sample['role']))
    tag = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return get_hermes_home() / 'state' / 'fd_headroom' / f'{"probe-" if probe else ""}{tag}.json'


def has_incident(sample: dict, *, probe=False) -> bool:
    """Discover unresolved delivery/terminal state, not resolved historical cards."""
    state = _load(_path(sample, probe), recover=False)
    return bool(state.get('terminal') or (state.get('envelope') and not state.get('task_id')))


_producer_token = None


def _producer():
    """Birth-qualified token: renewed after exec/restart AND fork, without PID reuse."""
    global _producer_token
    if _producer_token is None or _producer_token[0] != os.getpid():
        import uuid
        _producer_token = (os.getpid(), uuid.uuid4().hex)
    return ':'.join(map(str, _producer_token))


def _load(path, *, recover=True) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('invalid fd incident state')
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        if not recover:
            return {}
        # Caller holds the identity flock. Preserve the unreadable bytes, once.
        quarantined = path.with_name(f'{path.name}.corrupt-{time.time_ns()}')
        path.replace(quarantined)
        logger.warning('fd-headroom: prior incident state unreadable; preserved at %s', quarantined)
        return {'state_recovered': True}


def _save(path, state):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(state))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _new_envelope(sample, state, wall, probe, board):
    profile = sample.get('profile') or _profile()
    # Unique even if an old card closed within the clock's timestamp precision.
    stamp = max(wall, state.get('incident_started_at', wall - 1) + .000001)
    started = datetime.fromtimestamp(stamp, UTC).isoformat()
    namespace = 'fd-headroom-probe' if probe else 'fd-headroom'
    key = f'{namespace}:{sample["host"]}:{profile}:{sample["role"]}:{started}'
    classes = sample['census'].get('classes', {})
    top = max(classes, key=classes.get, default='other')
    title = (f'{"[probe] " if probe else ""}fd headroom: {sample["host"]} {sample["role"]} '
             f'at {sample["count"]}/{sample["soft"]} fds ({top})')
    body = ('Process FD headroom warning.\n\n' + json.dumps(sample, sort_keys=True) +
            '\n\nInvestigate descriptor growth and the loaded core. Never signal/restart production '
            'services, change configured nofile limits or raise a hard limit for this alert. '
            'The guard may have raised only its own soft limit; see raised above.')
    if state.get('state_recovered'):
        body += '\n\nPrior incident state was unreadable; preserved locally for investigation.'
    args = dict(title=title, body=body, assignee='default', idempotency_key=key, runs_on=['pool:worker'])
    previous_terminal = state.get('last_terminal')
    state.clear()
    if previous_terminal:
        state['last_terminal'] = previous_terminal
    state.update(incident_started_at=stamp, task_id=None, last_comment_at=wall,
                 envelope=dict(id=key, to_gw='hub', coll='tasks', key=f'default/{board}/new',
                               action='create', args=args))


def report(sample: dict, *, probe=False, probe_board=None, now=None, client=None,
           state_path: Path | None = None) -> dict:
    """One serial 20s delivery per tick. Unknown outcomes preserve the frozen incident.

    Definitive rejected/failed/expired receipts latch locally, without further request I/O.
    Only a subsequent healthy sample followed by a trigger opens a NEW incident; the
    last terminal envelope/receipt stays in the local audit, never mutated or resent.
    Probe callers must discover a board without a dispatcher before supplying its slug.
    Never permit the production default board. Probes are not human asks.
    """
    import fcntl
    if probe and (not probe_board or probe_board == 'default'):
        raise ValueError('probe requires an explicitly verified non-default isolated board')
    board = probe_board if probe else 'default'
    from hermes_state_guard import _in_test_context
    if client is None and _in_test_context():
        return {'error': 'test process: delivery disabled', 'error_class': 'TestContext'}
    path = state_path or _path(sample, probe)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _load(path, recover=False)
        state = _load(path)
        wall = time.time() if now is None else now
        if sample.get('raised'):
            state['raises'] = (state.get('raises', []) + [sample['raised']])[-8:]
        from hermes_cli.fd_headroom_transport import TerminalRequest
        transport = client
        try:
            if state.get('terminal'):
                producer = _producer()
                rearmed = state.get('terminal_rearmed_by', [])
                if not sample.get('triggered'):
                    # A sibling's low count says nothing about this producer's growth.
                    # Bound stale births; eviction can only prevent, never grant, rearm.
                    state['terminal_rearmed_by'] = (list(dict.fromkeys([*rearmed, producer])))[-64:]
                    state['terminal_rearmed'] = True  # Legacy diagnostic, not authority.
                if not sample.get('triggered') or producer not in rearmed:
                    _save(path, state)
                    return state  # No identity/transport I/O after a definitive receipt.
                state['last_terminal'] = {'envelope': state['envelope'], 'receipt': state['terminal']}
                _new_envelope(sample, state, wall, probe, board)
            if transport is None:
                from hermes_cli.fd_headroom_transport import Client
                transport = Client()
            _report(sample, state, wall, probe, board, transport, path)
            state.pop('error', None)
            state.pop('error_class', None)
            state.pop('failing_since', None)
        except TerminalRequest as exc:
            state['terminal'] = {'state': exc.state, 'result': exc.result}
            state.setdefault('failing_since', wall)
            state.update(error=str(exc)[:1000], error_class='TerminalRequest')
            _save(path, state)  # Retain the exact envelope and receipt before logging once.
            logger.error('fd-headroom: terminal card create state=%s result=%s incident=%s',
                         exc.state, str(exc.result)[:1000], state['envelope']['id'])
        except Exception as exc:  # health: allow BLE001 -- diagnostic delivery cannot kill a service tick
            state.setdefault('failing_since', wall)
            state.update(error=str(exc)[:1000], error_class=type(exc).__name__)
            if wall - state.get('last_error_log_at', -HOUR) >= HOUR:
                logger.warning('fd-headroom: card create/report failed: %s', exc)
                state['last_error_log_at'] = wall
        finally:
            if transport is not None:
                transport.close()
        _save(path, state)
        return state


class PendingVisibility(RuntimeError):
    """Creation was acknowledged, but the Board has not replicated the card yet."""


def _report(sample, state, wall, probe, board, client, path):
    tid = state.get('task_id')
    if tid:
        evidence = client.read(tid, board)  # Failed read propagates; never confused with absence.
        if evidence['state'] in {'never_existed', 'unknown'}:
            state.setdefault('card_unreplicated_since', wall)
            raise PendingVisibility('card not yet replicated')
        state.pop('card_unreplicated_since', None)
        row = evidence.get('row')
        opened = evidence['state'] == 'live' and row['status'] not in {'done', 'archived'}
        if opened:
            if not sample.get('triggered') and not state.get('raises'):
                return
            if wall - state['last_comment_at'] < HOUR:
                return
            state['last_comment_at'] = wall
            _save(path, state)  # Reserve the hour before ambiguous side effects.
            text = json.dumps({**sample, 'raises': state.get('raises', [])}, sort_keys=True)
            client.comment(dict(id=f'{state["envelope"]["id"]}:comment:{int(wall)}',
                                to_gw='hub', coll='tasks', key=f'default/{board}/{tid}',
                                action='comment', args={'text': f'fd-headroom: {text}'}))
            state['raises'] = []
            return
        # Only successful done/archived/tombstone evidence permits a new key.
        state.pop('task_id', None)
        state.pop('envelope', None)
        if not sample.get('triggered'):
            return
        _new_envelope(sample, state, wall, probe, board)
    elif not state.get('envelope'):
        if not sample.get('triggered'):
            return
        _new_envelope(sample, state, wall, probe, board)
    _save(path, state)  # Persist immutable intent BEFORE a potentially uncertain create.
    tid = client.create(state['envelope'])
    if tid:
        state['task_id'] = tid


def failure_note(state: dict, now=None) -> str:
    since = state.get('failing_since')
    if since is not None and (time.time() if now is None else now) - since >= HOUR:
        stamp = datetime.fromtimestamp(since, UTC).isoformat()
        return f'card create failing since {stamp}: {state.get("error", "unknown")}'
    return ''
