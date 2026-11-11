"""Dispatcher subprocess: submit one claim to its profile owner; never run an agent."""
# Spawned as a bare ``sys.executable -m``: in a PM install that interpreter carries no dependencies
# until hermes_bootstrap selects the committed environment, so it must be the first import.
try:
    import hermes_bootstrap
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap" and not "hermes_bootstrap".startswith(f"{exc.name}."):
        raise

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Optional

from hermes_cli.gateway_client import GatewayClientError as _GatewayClientError

# [daveey] The promise hermes-sync's ``kanban_workers_restart_safe`` asks of this core: a gateway restart
# (the one in ``hermes update``) neither fails nor strands a running Kanban run. The run CONTINUES: the
# old owner checkpoints the turn its restart Stop ends (``gateway.session_kanban_restart``), this worker
# keeps its pid and claim, waits up to ``CONTINUE_WAIT_S`` for the next owner and asks ``kanban.run`` for
# the same attempt again, and that owner continues the turn in the same session
# (``gateway.session_kanban_continue``), so no reclaim, crash or failure is booked. A draining owner's
# refusals refresh the silence budget up to its own after-turn cap (``_Claim.note_refusal``). Only when
# continuation cannot be proven safe (no checkpoint, the owner refuses it, or no owner within the wait)
# does the worker raise ``OwnerRestartLost`` and exit ``KANBAN_OWNER_RESTART_EXIT_CODE``, which the
# dead-worker sweep requeues as ``reclaimed`` with no failure counted. hermes-sync reads this
# file's text with the regex ``^GATEWAY_RESTART_SAFE\s*(?::\s*bool\s*)?=\s*True\b``: without a match every
# core-update drain holds new Kanban claims for up to 20 min. Pinned by
# tests/hermes_cli/test_kanban_worker_owner_restart.py.
GATEWAY_RESTART_SAFE = True

# A worker that loses its owner (a dropped socket, a restart, `hermes update` draining it or a new
# owner still starting) waits for it instead of exiting: the condition is the host's, not the card's,
# and a failed exit here spends the card's failure budget and lets the owner end the turn. Asking
# again is safe: ``kanban.run`` is idempotent per attempt and ``prompt.receipt`` is a read.
# ``OWNER_WAIT_S`` is the longest the owner may stay silent between answers before the worker
# gives up (the first ``kanban.run`` answer has its own wait, ``admission_answer_budget``).
OWNER_WAIT_S = 180.0
# [daveey] After the owner checkpointed this attempt's turn for its restart, the silence the worker waits
# out for the next owner: the old owner's exit, ``hermes update``'s tree move and the new owner's start.
CONTINUE_WAIT_S = 300.0
# A restart storm never loops one attempt forever: past this many checkpoints it is requeued uncounted.
MAX_CONTINUATIONS = 8
_TRANSIENT_OWNER_STATES = frozenset({'draining', 'starting'})
# In-band owner refusals that leave the attempt intact: a draining owner admits nothing new, and a
# retired authority's successor holds the admission the next connection reads.
_TRANSIENT_RPC_REASONS = frozenset({'runtime_draining', 'runtime_retired'})
# Control-socket answers a restarting owner gives (an ``inaccessible`` owner's reason, from
# discovery or the ticket fetch): a reset or half-written answer, or a replaced owner refusing a
# stale instance. Unsafe nodes or peers, a refused authorization, a bad pointer or a failed start
# stay final.
_RESTART_CONTROL_REASONS = frozenset({'invalid_control_peer', 'incomplete_control_response',
                                      'invalid_control_response'})
# Past the board lock wait: the owner's session setup and receipt before and after the write.
ADMISSION_ANSWER_MARGIN_S = 60.0
# [daveey] Owner refusals of ``kanban.run`` that are the host's own condition, not the card's: its
# runtime store or session policy could not be read. hermes-hub 2026-11-01 12:33-12:38 PST: a
# near-empty config.yaml left the session policy with no model, ``restore_policy`` refused it, and 17
# runs exited 1 on ``storage_unavailable`` before any admission; failure_limit=2 sent ten unrelated
# cards to triage. Before an admission the worker asks again (``kanban.run`` is idempotent per
# attempt), and an owner that keeps refusing for ``OWNER_WAIT_S`` ends the run owner-unavailable (69:
# requeued uncounted, bounded by ``NEUTRAL_STREAK_LIMIT``). Once admitted a turn may be running, so the
# refusal stays final there.
_HOST_REFUSAL_REASONS = frozenset({'storage_unavailable'})


def admission_answer_budget():
    """Seconds to wait for ``kanban.run``'s answer: the longest the owner's admission write can
    wait on the board lock (``write_txn`` retries ``BEGIN IMMEDIATE`` ``_BUSY_MAX_RETRIES`` times,
    each under the busy timeout) plus ``ADMISSION_ANSWER_MARGIN_S``."""
    from hermes_cli import kanban_db_connect
    lock_wait = (kanban_db_connect._BUSY_MAX_RETRIES + 1) * kanban_db_connect._resolve_busy_timeout_ms() / 1000
    return lock_wait + ADMISSION_ANSWER_MARGIN_S


def restart_wait_budget():
    """Seconds a drain's ``draining``/``starting`` answers keep refreshing the worker's silence budget.

    That owner is alive and holds this attempt's turn until the turn ends or its own cap
    (``agent.restart_after_turn_timeout``) force-drains it, then stops; the ``OWNER_WAIT_S`` of
    silence the worker still allows after this covers the stop and the next owner's start, so a
    continuous drain ends the worker at cap + ``OWNER_WAIT_S``. Counting the refusal as silence ended every Kanban turn a restart
    waited on ``OWNER_WAIT_S`` into the wait (2026-10-30 hub: 11 runs, all booked crashed). [daveey]"""
    from gateway.restart import parse_restart_after_turn_timeout
    raw = os.environ.get('HERMES_RESTART_AFTER_TURN_TIMEOUT', '').strip() or None
    if raw is None:
        try:
            from hermes_cli.config import load_config
            raw = (load_config().get('agent') or {}).get('restart_after_turn_timeout')
        except Exception:  # health: allow BLE001 -- an unreadable config falls back to the default cap
            raw = None
    return parse_restart_after_turn_timeout(raw)


class OwnerRestartLost(_GatewayClientError):
    """The owner's restart ended this attempt: its restart wait outlasted ``restart_wait_budget`` or
    the next owner reports the turn ``unknown``. Exits ``KANBAN_OWNER_RESTART_EXIT_CODE``, which the
    dead-worker sweep requeues without counting a failure. [daveey]"""


@dataclass
class _Claim:
    """What the owner has said about this attempt, carried across connections."""
    answered: float  # monotonic time of the owner's last answer
    session_id: Optional[str] = None
    admission_id: Optional[str] = None
    waiting: Optional[float] = None  # monotonic time the owner's current restart wait was first seen
    alive: float = float('-inf')  # monotonic time the owner last refused us from inside that wait
    restarted: bool = False  # the owner restarted (or began to) during this attempt
    continuing: bool = False  # the owner checkpointed the turn: the next ``kanban.run`` asks to continue it
    checkpoints: tuple = ()  # checkpoint tokens already continued (each is continued at most once)

    def silence_budget(self):
        return CONTINUE_WAIT_S if self.continuing else OWNER_WAIT_S

    def continue_after(self, token):
        """The owner checkpointed this attempt's turn: ask the next owner to continue it. [daveey]"""
        self.checkpoints = (*self.checkpoints, token)
        self.continuing = self.restarted = True
        self.admission_id = None
        self.answered = time.monotonic()

    def silent_for(self):
        return time.monotonic() - max(self.answered, self.alive)

    def note_refusal(self, exc):
        """An owner refusing us because it is draining (a restart's after-turn wait, ``hermes update``
        pausing it) answered over its live control socket or session: it is alive and still runs the
        turn, so it refreshes the silence budget until the drain outlasts the owner's own cap
        (``restart_wait_budget``). ``starting`` and a lost transport only mark the restart."""
        from hermes_cli.gateway_client import GatewayRPCError, GatewayUnavailableError
        if isinstance(exc, GatewayRPCError):
            draining = str(exc) in _TRANSIENT_RPC_REASONS
        elif isinstance(exc, GatewayUnavailableError) and exc.state in _TRANSIENT_OWNER_STATES:
            # draining comes only from a live control-socket identify (restart_wait) or a live update
            # fence (update_paused); starting only from a strictly held gateway.lock. An idle exit
            # is an owner leaving with no work, so not one running this turn.
            draining = exc.reason_code != 'idle_exit'
        else:
            return
        self.restarted = self.restarted or self.continuing or self.admission_id is not None
        if not draining:
            return
        now = time.monotonic()
        if self.waiting is None or self.waiting < self.answered:
            self.waiting = now  # a new drain (the owner answered since the last one)
        if now - self.waiting < restart_wait_budget():
            self.alive = now


async def run(params, board_db):
    from agent.retry_utils import jittered_backoff
    from hermes_cli.gateway_client import connect_gateway, GatewayClientError, outside_restart_wait
    from websockets.exceptions import WebSocketException
    if os.environ.get('HERMES_TUI_GATEWAY_URL'):
        raise GatewayClientError('Kanban requires the assigned local profile owner')
    # [daveey] A capsule card may only reach an owner running this submitter's own capsule-gated core;
    # any other owner is refused here, before ``kanban.run`` (no session, admission, model or tool work).
    from hermes_cli.kanban_capsule_owner import card_requires_capsule, owner_check
    check = owner_check(await asyncio.to_thread(card_requires_capsule, board_db, params['task_id']))
    # A lost socket is not a lost attempt, and exiting here would let ``_watch_submitter`` end the
    # owner's in-flight turn, so the next connection resumes whatever the last one reached. The
    # wait is bounded from the owner's last answer: an owner that keeps answering keeps the
    # worker, and one that stays silent for ``OWNER_WAIT_S`` fails the worker honestly. An owner
    # refusing us from its restart wait counts as answering, up to that wait's own cap.
    claim = _Claim(answered=time.monotonic())
    failures = 0
    while True:
        seen = claim.answered
        try:
            # Never into a restart's after-turn wait: it cut this socket so the turn's submitter
            # waits for the NEXT owner, and a worker polling its receipt inside the wait keeps the
            # turn it waits on alive, holding `hermes update` until restart_after_turn_timeout.
            with outside_restart_wait():
                async with connect_gateway(**({'owner_check': check} if check else {})) as client:
                    # kanban.run subscribes this socket to the card's session, but only receipts
                    # matter here: discard the turn's events, or they fill the client's bounded
                    # queue and its reader stops, with the next receipt behind it.
                    drain = asyncio.create_task(_discard(client.events))
                    try:
                        receipt = await _await_receipt(client, params, board_db, claim)
                    finally:
                        drain.cancel()
        # A restart also surfaces as a refused or reset dial, a failed handshake, or an answer
        # that never came.
        except (GatewayClientError, WebSocketException, OSError, TimeoutError) as exc:
            if _owner_down(exc, claim):
                # [daveey] The owner's service is failed/masked/disabled (an operator's act, not a blip):
                # redialing cannot help and the card did nothing wrong. Exit ``KANBAN_OWNER_DOWN_EXIT_CODE``
                # at once; the sweep requeues it uncounted and the dispatcher holds the card
                # (``kanban_owner_down``). t_01a19b8ff5c2ddc9 was booked crashed twice for this.
                exc.owner_down = True
                raise
            host_refusal = _host_refusal(exc, claim)
            if not (host_refusal or _restartable(exc)):
                if claim.continuing:
                    # The next owner would not continue the checkpointed turn (``kanban_continuation_unsafe``,
                    # a moved claim): today's uncounted requeue, which resumes this transcript. [daveey]
                    raise OwnerRestartLost(f'Kanban owner did not continue the checkpointed turn: {exc}') from exc
                raise
            claim.note_refusal(exc)
            if claim.silent_for() >= claim.silence_budget():
                # WebSocket errors can embed remote bodies; only our own messages are quoted.
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = exc if isinstance(exc, GatewayClientError) else type(exc).__name__ + (
                    f' (HTTP {status})' if status else '')
                verb = 'kept refusing' if host_refusal else 'silent'
                message = f'Kanban owner {verb} for {claim.silent_for():.0f}s: {detail}'
                if claim.restarted:
                    # An ADMITTED turn lost to the owner's restart: exit 76, requeued uncounted. [daveey]
                    raise OwnerRestartLost(message) from exc
                error = GatewayClientError(message)
                # [daveey] No admission answer reached us: the host's condition, not the card's. The
                # exit says so and the dispatcher requeues the run uncounted unless the board shows
                # the owner did admit it (t_82f0fe15 runs 7630/7649: two restart_wait refusals
                # spent its failure budget and sent it to triage).
                error.owner_unavailable = claim.admission_id is None
                # [daveey] An owner that kept refusing on its own host state (not silent): the sweep
                # files the host notice from this typed reason (``kanban_host_refusal``).
                error.host_refusal = str(exc) if host_refusal else None
                raise error from exc
        else:
            token = None if receipt['status'] != 'terminal' else await asyncio.to_thread(
                _checkpoint, params, board_db, claim)
            if token is not None:
                print('Kanban owner restarting: turn checkpointed; waiting for the next owner to continue it',
                      file=sys.stderr, flush=True)
                claim.continue_after(token)
                failures = 0
                await asyncio.sleep(jittered_backoff(1, base_delay=1.0, max_delay=8.0, jitter_ratio=1.0))
                continue
            if receipt['status'] == 'unknown' and claim.restarted:
                raise OwnerRestartLost('Kanban execution unknown after an owner restart; retained by owner')
            return _exit_code(receipt, params, board_db)
        failures = 1 if claim.answered != seen else failures + 1
        # Jittered (1-2 s first, up to 16 s): workers of one dead owner do not all redial, and so
        # start a replacement, in the same instant.
        await asyncio.sleep(jittered_backoff(failures, base_delay=1.0, max_delay=8.0, jitter_ratio=1.0))


def _checkpoint(params, board_db, claim):
    """The token of a turn checkpoint the owner booked as this attempt's result (``continuable``) that this
    worker has not continued yet, or None (the result is final). [daveey]"""
    import sqlite3
    from gateway.session_kanban import worker_result
    try:
        result = worker_result(board_db, params)
    except (OSError, sqlite3.Error, ValueError, KeyError):
        return None
    token = result.get('checkpoint')
    if (result.get('continuable') is not True or not isinstance(token, str) or not token
            or token in claim.checkpoints or len(claim.checkpoints) >= MAX_CONTINUATIONS):
        return None
    return token


def _restartable(exc):
    """False when *exc* is the owner's own final answer; True for a restarting owner or a lost transport."""
    from hermes_cli.gateway_client import GatewayRPCError, GatewayUnavailableError
    if isinstance(exc, GatewayRPCError):
        return str(exc) in _TRANSIENT_RPC_REASONS
    if isinstance(exc, GatewayUnavailableError):
        return exc.state in _TRANSIENT_OWNER_STATES or exc.reason_code in _RESTART_CONTROL_REASONS
    return True


def _owner_down(exc, claim):
    """True for a persistent pre-admission service state (``kanban_owner_down.PERSISTENT_CODES``). [daveey]"""
    from hermes_cli.gateway_client import GatewayUnavailableError
    from hermes_cli.kanban_owner_down import persistent
    return (claim.admission_id is None and not claim.continuing and isinstance(exc, GatewayUnavailableError)
            and persistent(exc.reason_code))


def _host_refusal(exc, claim):
    """True for an owner refusal that is the host's condition (``_HOST_REFUSAL_REASONS``) while no
    admission exists: nothing ran, so asking again is safe. [daveey]"""
    from hermes_cli.gateway_client import GatewayRPCError
    return (claim.admission_id is None and not claim.continuing and isinstance(exc, GatewayRPCError)
            and str(exc) in _HOST_REFUSAL_REASONS)


async def _discard(events):
    while True:
        await events.get()


async def _await_receipt(client, params, board_db, claim):
    """Submit the claim (or, on a later connection, re-read the admission it got) and return its
    settled receipt, recording each owner answer on *claim*."""
    if claim.admission_id is None:
        db = str(await asyncio.to_thread(Path(board_db).resolve))
        # The owner answers only after it checks the claim and writes its ``owner_admitted`` marker
        # in one board write transaction. The board (the cross-profile dispatch bus) can make that
        # wait out its busy timeout, so the 30 s default left the owner admitting an attempt whose
        # submitter had exited (the later-bound worker's submitter-liveness watcher ended it, and the
        # admission was left ``unknown``). The wait is still finite: once the marker commits,
        # ``enforce_max_runtime`` and TTL reclaim skip the run (``owner_reclaim_paused``), so an owner
        # that stalls after admitting is bounded by this wait and ``OWNER_WAIT_S``.
        accepted = await client.rpc('kanban.run', _timeout=admission_answer_budget(), **dict(params, db=db))
        receipt = accepted['receipt']
        claim.session_id, claim.admission_id = accepted['session_id'], receipt['admission_id']
        print(f'Session: {claim.session_id}', file=sys.stderr)
    else:
        receipt = await _receipt(client, claim)
    claim.answered = time.monotonic()
    while receipt['status'] not in {'terminal', 'unknown'}:
        await asyncio.sleep(.2)
        receipt = await _receipt(client, claim)
        claim.answered = time.monotonic()
    return receipt


def _exit_code(receipt, params, board_db):
    from hermes_cli.gateway_client import GatewayClientError
    from gateway.session_kanban import worker_result
    if receipt['status'] == 'unknown':
        # [daveey] A worker that died at start booked its reason on the attempt: quote it, so the
        # crash record names the cause instead of only the owner's unknown receipt.
        import sqlite3
        try:
            cause = worker_result(board_db, params).get('last_output') or ''
        except (OSError, sqlite3.Error, ValueError, KeyError):
            cause = ''
        raise GatewayClientError('Kanban execution unknown; retained by owner' + (f' ({cause})' if cause else ''))
    result = worker_result(board_db, params)
    # The attempt's reply ends the log, as a one-shot worker's did: crash diagnostics quote it.
    if result.get('last_output'):
        # Flushed: stdout is block-buffered into the log file, and an unflushed reply landed after
        # this run's (stderr) exit trailer, where the next run's crash record quoted it.
        print(result['last_output'], flush=True)
    return result.get('exit_code', 1)


async def _receipt(client, claim):
    return await client.rpc('prompt.receipt', session_id=claim.session_id, admission_id=claim.admission_id)


def main():
    # The dead-worker sweep books this process's exit from the trailer in its log (it never
    # reaps us), so every exit path writes it — the same contract as a quiet one-shot worker.
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    rc = 1
    try:
        rc = _submit()
    finally:
        print(f'\n{KANBAN_WORKER_EXIT_TRAILER}{int(rc)}', file=sys.stderr, flush=True)
    return rc


def _book_refusal(params, board_db, rc, line, proof=None):
    """Book this submitter's refusal as its run's receipt (``record_submitter_result``): a bound
    run's crash diagnosis never trusts the shared task log, so the line printed below would
    otherwise be lost to the owner-death classifier. *proof*: a pre-admission host refusal
    (``kanban_host_refusal.preadmission_proof``), stored on the receipt. Never raises: the exit must
    not depend on it. [daveey]"""
    if params is None or not board_db:
        return
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        from hermes_cli.kanban_host_refusal import PROOF_KEY
        from hermes_cli.kanban_worker_log import record_submitter_result
        # The proof only when there is one: the plain refusal keeps its call shape.
        extra = {'extra': {PROOF_KEY: proof}} if proof else {}
        with connect_closing(Path(board_db)) as conn, kb.write_txn(conn):
            record_submitter_result(conn, params, os.getpid(), rc, line, **extra)
    except Exception as exc:  # health: allow BLE001 -- preserve the original refusal exit on receipt-store failure
        print(f'Kanban refusal receipt could not be recorded: {type(exc).__name__}', file=sys.stderr)


def _submit():
    params = board_db = None
    try:
        params = {'board': os.environ['HERMES_KANBAN_BOARD'], 'task_id': os.environ['HERMES_KANBAN_TASK'],
            'run_id': int(os.environ['HERMES_KANBAN_RUN_ID']), 'claim_lock': os.environ['HERMES_KANBAN_CLAIM_LOCK']}
        board_db = os.environ['HERMES_KANBAN_DB']
        # Owner startup must never inherit this worker's task/tool/goal identity.
        for key in list(os.environ):
            # The busy timeout is a host tuning knob, not task identity: the answer budget reads it.
            if key.startswith('HERMES_KANBAN_') and key not in ('HERMES_KANBAN_HOME', 'HERMES_KANBAN_BUSY_TIMEOUT_MS'):
                os.environ.pop(key)
        for key in ('HERMES_SESSION_SOURCE', 'HERMES_TENANT', 'TERMINAL_CWD'):
            os.environ.pop(key, None)
        return asyncio.run(run(params, board_db))
    except OwnerRestartLost as exc:
        # The owner's restart, not the card: the sweep requeues this exit without a failure. [daveey]
        from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE
        print(f'Kanban owner restarted: {exc}', file=sys.stderr)
        return KANBAN_OWNER_RESTART_EXIT_CODE
    except (KeyError, ValueError, OSError) as exc:
        line = f'Kanban owner refused: {exc}'
        print(line, file=sys.stderr)
        rc = 1
        proof = None
        if getattr(exc, 'owner_down', False):
            from hermes_cli.kanban_owner_down import KANBAN_OWNER_DOWN_EXIT_CODE
            rc = KANBAN_OWNER_DOWN_EXIT_CODE
        elif getattr(exc, 'owner_unavailable', False):
            from hermes_cli.kanban_owner_unavailable import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            from hermes_cli.kanban_host_refusal import preadmission_proof
            rc = KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            proof = preadmission_proof(getattr(exc, 'host_refusal', None))
        _book_refusal(params, board_db, rc, line, proof)
        return rc


if __name__ == '__main__':
    raise SystemExit(main())
