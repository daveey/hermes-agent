"""Durable origin obligations and readback receipts, independent of the hub replica.

This module owns persistence, not transport. Callers supply the authenticated
courier sender and the origin gateway/profile from local configuration. Request
IDs are globally unique in that courier (its create API rejects envelope reuse).
Binding IDs are task-owner-qualified; each binding is an immutable revision,
not an update command. A version change requires a new binding ID. Observations,
verified release, lineage refresh and integration with projections are separate
lifecycle operations, not implied by an accepted binding receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable

from hermes_cli.kanban_scope_heartbeat import SCOPE_HEARTBEAT_FLOOR_S, scope_window


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_binding(binding: dict) -> None:
    """Reject incomplete/ambiguous identities before writing an acknowledgement."""
    try:
        if not isinstance(binding, dict) or set(binding) != {"binding_id", "version", "owner", "origin", "task_record_key", "publication_version"}:
            raise ValueError
        for field in ("version", "publication_version"):
            if type(binding[field]) is not int or binding[field] < 1:
                raise ValueError
        for field, keys in (("owner", {"gateway", "profile", "board", "task_id"}),
                            ("origin", {"gateway", "profile", "session"})):
            if not isinstance(binding[field], dict) or set(binding[field]) != keys:
                raise ValueError
            for value in binding[field].values():
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", value):
                    raise ValueError
        if not isinstance(binding["binding_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", binding["binding_id"]):
            raise ValueError
        owner = binding["owner"]
        if binding["task_record_key"] != f"{owner['profile']}/{owner['board']}/{owner['task_id']}":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("origin_binding_invalid") from None


def binding_hash(binding: dict) -> str:
    validate_binding(binding)
    return hashlib.sha256(_json(binding).encode()).hexdigest()


@contextmanager
def _db(path: Path, *, timeout: float = 5):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_obligations (
            binding_id TEXT PRIMARY KEY, binding_hash TEXT NOT NULL,
            binding_json TEXT NOT NULL, resolved_session TEXT NOT NULL,
            task_json TEXT NOT NULL, observation_version INTEGER NOT NULL,
            released INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_receipts (
            request_id TEXT PRIMARY KEY NOT NULL, binding_hash TEXT NOT NULL,
            receipt_json TEXT NOT NULL)""")
        conn.commit()
        yield conn
    finally:
        conn.close()


def _binding_key(binding: dict) -> str:
    # IDs belong to the task owner, not to a global unqualified namespace.
    return _json([binding["owner"], binding["binding_id"]])


def _read_receipt(conn: sqlite3.Connection, request_id: str) -> dict | None:
    row = conn.execute("SELECT receipt_json FROM origin_receipts WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        return None
    ack = json.loads(row[0])
    obligation = conn.execute("SELECT binding_hash FROM origin_obligations WHERE binding_id=?", (_binding_key(ack),)).fetchone()
    if not obligation or obligation[0] != ack["binding_hash"]:
        raise ValueError("origin_obligation_missing")
    return ack


def receipt(path: Path, request_id: str) -> dict | None:
    with _db(path) as conn:
        return _read_receipt(conn, request_id)


def obligations(path: Path, *, gateway: str, profile: str, sessions: list[str]) -> list[dict]:
    result = []
    with _db(path) as conn:
        for raw, resolved, task, version, released in conn.execute(
                "SELECT binding_json,resolved_session,task_json,observation_version,released FROM origin_obligations"):
            binding = json.loads(raw)
            origin = binding["origin"]
            if (origin["gateway"] == gateway and origin["profile"] == profile
                    and (origin["session"] in sessions or resolved in sessions)):
                payload = json.loads(task)
                released = bool(released and _owner_evidence(payload).get('verified') is True)
                result.append({**binding, "resolved_session": resolved, "task": payload,
                               "observation_version": version, "unfinished": not released})
    return result


def observe_owner_task(conn: sqlite3.Connection, task: dict, *, now: float) -> dict:
    """Attach owner evidence during the collector's existing read transaction.

    The collector must call this on the owner, never on a replica. A completion
    event is evidence of the core lifecycle gate; status alone (including an
    archive) is not. Goal-mode also requires a current, completion-bound
    judge-success receipt; unavailable judges never supply release evidence.
    """
    if not conn.in_transaction:
        raise ValueError('origin_observation_snapshot_required')
    row = conn.execute(
        "SELECT status,completed_at,current_run_id,goal_mode,completion_contract FROM tasks WHERE id=?",
        (task['id'],)).fetchone()
    if row is None or row[0] != task.get('status'):
        raise ValueError('origin_observation_task_changed')
    supplied = task.get('origin_bindings', [])
    has_intents = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='origin_creation_intents'").fetchone()
    authoritative = [json.loads(r[0]) for r in conn.execute(
        'SELECT binding_json FROM origin_creation_intents WHERE task_id=?', (task['id'],))] if has_intents else []
    if (not isinstance(supplied, list) or any(not isinstance(m, dict) for m in supplied)
            or sorted(_json({k: v for k, v in m.items() if k != 'publication_version'}) for m in supplied)
            != sorted(_json(m) for m in authoritative)
            or any(m.get('owner') != {**{k: task.get(k) for k in ('gateway', 'profile', 'board')},
                                       'task_id': task['id']} for m in authoritative)):
        raise ValueError('origin_observation_binding_changed')
    event = conn.execute(
        "SELECT id,run_id,created_at,payload FROM task_events WHERE task_id=? AND kind='completed' ORDER BY id DESC LIMIT 1",
        (task['id'],)).fetchone()
    run = conn.execute(
        "SELECT id,status,outcome,ended_at,summary FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task['id'],)).fetchone()
    terminal = row[0] == 'done'
    if row[0] == 'archived' and event:
        archived = conn.execute(
            "SELECT id,payload FROM task_events WHERE task_id=? AND kind='archived' ORDER BY id DESC LIMIT 1",
            (task['id'],)).fetchone()
        archive_payload = json.loads(archived[1] or '{}') if archived else None
        terminal = bool(archived and archived[0] > event[0] and isinstance(archive_payload, dict)
                        and archive_payload.get('source_status') == 'done')
    contract_verified = row[4] == 'local-only'
    if event and not contract_verified:
        completed = json.loads(event[3] or '{}')
        acceptance_id = completed.get('pr_acceptance_event_id') if isinstance(completed, dict) else None
        acceptance = conn.execute(
            "SELECT payload,run_id FROM task_events WHERE id=? AND task_id=? AND kind='pr_acceptance' AND id<?",
            (acceptance_id, task['id'], event[0])).fetchone() if type(acceptance_id) is int else None
        if acceptance and acceptance[1] is not None and acceptance[1] == event[1]:
            receipt = json.loads(acceptance[0] or '{}')
            # [daveey] A contract_waiver is accepted only on a no_required_checks receipt (kanban_pr_acceptance_store).
            waived = (receipt.get('classification') == 'no_required_checks'
                      and isinstance(receipt.get('waived'), dict)) if isinstance(receipt, dict) else False
            contract_verified = bool(isinstance(receipt, dict)
                                     # CLOSED is the gate's accepted disposition (not delivery), like success.
                                     and ((receipt.get('ok') is True
                                           and receipt.get('classification') in ('success', 'closed'))
                                          or waived)
                                     and receipt.get('pr_url') == row[4]
                                     and isinstance(receipt.get('head_sha'), str)
                                     and re.fullmatch(r'[0-9a-f]{40}', receipt['head_sha']))
    goal_verified = not row[3]
    if row[3] and event:
        completed = json.loads(event[3] or '{}')
        receipt_id = completed.get('goal_verified_event_id') if isinstance(completed, dict) else None
        receipt = conn.execute(
            "SELECT payload,run_id FROM task_events WHERE id=? AND task_id=? AND kind='goal_verified' AND id<?",
            (receipt_id, task['id'], event[0])).fetchone() if type(receipt_id) is int else None
        if receipt and receipt[1] is not None and receipt[1] == event[1]:
            from hermes_cli.kanban_db import _first_line
            from hermes_cli.kanban_goal_receipt import digest
            proof = json.loads(receipt[0] or '{}')
            subject = proof.get('subject') if isinstance(proof, dict) else None
            current = conn.execute('SELECT title,body FROM tasks WHERE id=?', (task['id'],)).fetchone()
            recorded = (completed.get('summary') or completed.get('result') or '').strip()
            judged = subject.get('evidence') if isinstance(subject, dict) else None
            # [daveey] The completed event keeps only the handoff's first line (400 chars). A longer or multi-line
            # handoff is bound through the closing run, which stores it whole, so it can still match the judged
            # evidence; comparing against the event alone never released such a card.
            # The event preview hides FINAL SEND blocks (public_summary), so accept that form of the first line too.
            from hermes_cli.kanban_final_send import public_summary
            evidence_ok = isinstance(judged, str) and (judged == recorded or (
                recorded in (_first_line(judged, 400).strip(), (_first_line(public_summary(judged), 400) or '').strip())
                and run is not None and run[0] == event[1]
                and judged == (run[4] or '').strip()))
            goal_verified = bool(isinstance(subject, dict) and proof.get('snapshot') == digest(subject)
                                 and subject.get('task_id') == task['id']
                                 and subject.get('title') == current[0] and subject.get('body') == current[1]
                                 and subject.get('contract') == row[4]
                                 and evidence_ok)
    run_closed = bool(run and run[1] in ('done', 'completed') and run[2] == 'completed'
                      and row[1] is not None and run[3] is not None and run[3] >= row[1])
    verified = bool(terminal and row[2] is None and goal_verified and contract_verified
                    and event and run_closed and event[1] == run[0] and event[2] >= row[1])
    legacy = False
    if not verified and row[2] is None and run_closed and (event or row[0] == 'done'):
        # [daveey] Completed before this board's first judge receipt: no receipt could exist then, and holding the
        # card ``unknown`` forever left every origin session waiting on work nobody will produce. A completion
        # event (when the board still has it) must be the one that closed this run; a board whose completion
        # events were lost keeps only the closing run, which ``complete_task`` writes in the same transaction.
        # Receipt-era cards and boards that never recorded a receipt get no legacy pass.
        epoch = _receipt_epoch(conn)
        if epoch is not None:
            if event:
                legacy = bool(terminal and event[0] < epoch[0] and event[1] == run[0] and event[2] >= row[1])
                contract_ok = contract_verified
            else:
                legacy = row[1] < epoch[1]
                contract_ok = row[4] == 'local-only'
            legacy = bool(legacy and contract_ok)
            verified = legacy
    payload = {k: v for k, v in task.items() if k != 'origin_observation'}
    if row[0] == 'blocked':
        from hermes_cli.kanban_block_notice import blocked_actor_fields
        payload.update(blocked_actor_fields(conn, task['id']))
    # Only post-rollout events opt in: do not replay the replica's historical done-card inventory.
    if terminal and event and json.loads(event[3] or '{}').get('notice_version') == 1:
        payload['completion_notice'] = {'event_id': event[0], 'created_at': event[2],
                                        'payload': json.loads(event[3] or '{}')}
    if row[0] in ('scheduled', 'blocked'):
        from hermes_cli.kanban_wait_replica import observe_wait
        payload['wait_observation'] = observe_wait(conn, task['id'], now=now)
    # No per-task timestamp: an unchanged task must publish an identical record, or every sync republishes
    # every task. Owner liveness is the collector's per-scope heartbeat (``task_scopes[].observed_at``).
    observation = {
        'schema': 1,
        'task_hash': hashlib.sha256(_json(payload).encode()).hexdigest(),
        'verified': verified,
    }
    if legacy:
        observation['basis'] = 'legacy_completed'  # only here, so every other record publishes unchanged
    return {**payload, 'origin_observation': observation}


# db file:inode -> (id, created_at) of its first ``goal_verified`` event. Immutable once it exists: events are append-only.
_receipt_epochs: dict[str, tuple[int, int]] = {}


def _receipt_epoch(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """``(event id, created_at)`` of the board's first judge receipt, or None when it never recorded one. [daveey]"""
    try:
        path = next((f for _, name, f in conn.execute('PRAGMA database_list') if name == 'main'), '') or ''
        main = f'{path}:{os.stat(path).st_ino}' if path else ''  # a swapped-in (recovered) file is a new board
    except (OSError, sqlite3.Error):
        main = ''
    if main and main in _receipt_epochs:
        return _receipt_epochs[main]
    found = conn.execute(
        "SELECT id,created_at FROM task_events WHERE kind='goal_verified' ORDER BY id LIMIT 1").fetchone()
    if found is None or type(found[0]) is not int or type(found[1]) is not int:
        return None
    if main:
        _receipt_epochs[main] = (found[0], found[1])
    return found[0], found[1]


def _recent(observed_at: object, now: float, window: float = 180) -> bool:
    return type(observed_at) in (int, float) and 0 <= now - observed_at <= window


def _park_of(task: dict) -> dict | None:
    """[daveey] Display-only run fields of an owner record (``kanban_wait_info.park_fields``); never raises."""
    try:
        from hermes_cli.kanban_wait_info import park_fields
        return park_fields(task.get('run')) if isinstance(task, dict) else None
    except Exception:
        return None


def _owner_evidence(task: dict) -> dict:
    evidence = task.get('origin_observation')
    if not isinstance(evidence, dict) or type(evidence.get('schema')) is not int or evidence['schema'] != 1:
        return {}
    payload = {k: v for k, v in task.items() if k != 'origin_observation'}
    if evidence.get('task_hash') != hashlib.sha256(_json(payload).encode()).hexdigest():
        return {}
    return evidence


def _verified_terminal(task: dict) -> bool:
    """The release invariant: a terminal owner record with verified completion evidence is released."""
    return (isinstance(task, dict) and task.get('status') in ('done', 'archived')
            and _owner_evidence(task).get('verified') is True)


def _stuck_unreleased(task_raw: str, released: int) -> bool:
    """[daveey] A row stored unreleased at a terminal, verified version (the pre-fix accept INSERT omitted
    ``released``). No newer replica version will ever arrive for it, so the version check cannot heal it."""
    if released:
        return False
    try:
        return _verified_terminal(json.loads(task_raw))
    except (ValueError, TypeError):
        return False


def _advanced_observation(binding_raw: str, observed: int, source: sqlite3.Connection) -> tuple | None:
    """``(task, version, released)`` when the replica holds a newer, exactly bound record for this obligation."""
    binding = json.loads(binding_raw)
    validate_binding(binding)
    row = source.execute(
        "SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
        (binding['owner']['gateway'], binding['task_record_key'])).fetchone()
    if not row or type(row[0]) is not int or row[0] <= observed:
        return None
    task = json.loads(row[1])
    owner = binding['owner']
    if (not isinstance(task, dict)
            or any(task.get(f) != owner[f] for f in ('gateway', 'profile', 'board'))
            or task.get('id') != owner['task_id']):
        return None
    marker = {k: v for k, v in binding.items() if k != 'publication_version'}
    markers = task.get('origin_bindings')
    if not isinstance(markers, list) or marker not in [
            {k: v for k, v in m.items() if k != 'publication_version'}
            for m in markers if isinstance(m, dict)]:
        return None
    return task, row[0], _verified_terminal(task)


def refresh_observations(path: Path, replica: Path, *, timeout: float = 5) -> None:
    """Advance durable observations only from newer, exactly bound owner records.

    Disappearance is not completion. Corrupt/unreadable stores raise to let callers
    display unknown, rather than returning an empty successful projection. A newer
    nonterminal publication reopens a previously released obligation.

    [daveey] The write lock is taken only when some observation would advance: a read-only scan decides first.
    Every projection in every process refreshes, so an unconditional ``BEGIN IMMEDIATE`` made readers collide with
    each other, and the loser's projection went stale for a poll.
    """
    if not path.is_file() or not replica.is_file():
        return
    from hermes_cli.kanban_origin import _ro

    source = _ro(replica)
    try:
        try:
            with closing(_ro(path)) as ledger:
                pending = any(
                    _stuck_unreleased(task_raw, released)
                    or _advanced_observation(raw, observed, source) is not None
                    for raw, observed, task_raw, released in ledger.execute(
                        "SELECT binding_json,observation_version,task_json,released FROM origin_obligations"
                    ).fetchall())
        except sqlite3.OperationalError:
            pending = True  # no table yet (or unreadable read-only): the locked path below decides
        if not pending:
            return
        with _db(path, timeout=timeout) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, raw, observed, task_raw, stored_released in conn.execute(
                    "SELECT binding_id,binding_json,observation_version,task_json,released"
                    " FROM origin_obligations").fetchall():
                advanced = _advanced_observation(raw, observed, source)
                if advanced is None:
                    if _stuck_unreleased(task_raw, stored_released):
                        conn.execute("UPDATE origin_obligations SET released=1 WHERE binding_id=?", (key,))
                    continue
                task, version, released = advanced
                conn.execute(
                    "UPDATE origin_obligations SET task_json=?,observation_version=?,released=? WHERE binding_id=?",
                    (_json(task), version, int(released), key))
    finally:
        source.close()


def _lock_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def project(path: Path, *, replica: Path | None, gateway: str, profile: str,
            now: float | None = None) -> list[dict]:
    """Owner-qualified receipt rows, including explicit unknown and released rows.

    Consumers filter this single projection, never infer release from an absent
    replica row. Old and resolved session aliases remain available for lineage
    joins. Replica failures are stale observations; ledger failures propagate.
    """
    import time
    from hermes_cli.kanban_origin import _ro

    if not path.exists():
        return []
    now = time.time() if now is None else now
    source = None
    fresh = False
    last_ok = None
    try:
        if replica is not None and replica.is_file():
            # Sync metadata is independent of the ledger write: retain its last
            # successful pull timestamp even when observation refresh is busy.
            source = _ro(replica)
            meta = dict(source.execute("SELECT k,v FROM meta"))
            last_ok = float(meta.get('last_ok') or 0)
            # Inventory readers must not queue behind a receipt/sync writer.
            try:
                refresh_observations(path, replica, timeout=0)
            except sqlite3.OperationalError as exc:
                # [daveey] Another process holds the ledger's write lock (it is advancing these same
                # observations). The stored ones are still evidence: below, a row is current only when the
                # replica holds exactly its stored version, so a row this refresh would have advanced reads
                # not-current on its own. Marking the whole replica stale here flickered every hub row.
                if not _lock_busy(exc):
                    raise
            fresh = (0 <= now - last_ok <= 180 and not meta.get('offline_since'))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # A failed refresh never erases a receipt. Reading the ledger below
        # still raises if the authoritative store itself is broken.
        fresh = False
    result = []
    try:
        with closing(_ro(path)) as conn:
            from hermes_cli.kanban_origin_legacy import aliases_for_projection
            alias_receipts = {}
            for receipt_raw, in conn.execute('SELECT receipt_json FROM origin_receipts'):
                ack = json.loads(receipt_raw)
                if 'legacy_alias' in ack:
                    alias_receipts.setdefault(ack['binding_hash'], []).append(ack)
            for raw, resolved, payload, version, released in conn.execute(
                    "SELECT binding_json,resolved_session,task_json,observation_version,released FROM origin_obligations"):
                binding = json.loads(raw)
                validate_binding(binding)
                origin, owner = binding['origin'], binding['owner']
                if (origin['gateway'], origin['profile']) != (gateway, profile):
                    continue
                task = json.loads(payload)
                # Old ledgers set released from status alone. Recheck evidence
                # even when the replica version has not advanced.
                released = bool(released and _owner_evidence(task).get('verified') is True)
                current = False
                if fresh and source is not None:
                    row = source.execute(
                        "SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                        (owner['gateway'], binding['task_record_key'])).fetchone()
                    current = bool(row and row[0] == version and json.loads(row[1]) == task)
                    # A successful origin pull does not prove the owner could
                    # read this board. Consume the collector's qualified scope
                    # inventory; missing/ambiguous inventory is not healthy.
                    inventory = source.execute(
                        "SELECT data FROM replica WHERE gw=? AND coll='gateway' AND key='info'",
                        (owner['gateway'],)).fetchone()
                    try:
                        info = json.loads(inventory[0]) if inventory else {}
                        scopes = info.get('task_scopes')
                        if not isinstance(scopes, list):
                            scopes = []
                        matching = [s for s in scopes if isinstance(s, dict)
                                    and (s.get('profile'), s.get('board')) == (owner['profile'], owner['board'])]
                        readable = (info.get('gateway') == owner['gateway'] and len(matching) == 1
                                    and matching[0].get('state') == 'ok')
                        heartbeat = matching[0].get('observed_at') if readable else None
                        window = scope_window(info)
                    except (ValueError, TypeError, AttributeError):
                        readable, heartbeat, window = False, None, SCOPE_HEARTBEAT_FLOOR_S
                    # Owner liveness: the collector's per-scope heartbeat (one record per sync) proves it read
                    # this whole board recently, so an unchanged, current task record is still observed. The
                    # per-task stamp is accepted too, for records published before scope heartbeats existed.
                    live = (_recent(heartbeat, now, window)
                            or _recent(_owner_evidence(task).get('observed_at'), now))
                    current = current and readable and live
                if (owner['gateway'], owner['profile']) == (gateway, profile):
                    from hermes_cli.kanban_origin_local import observe_local_binding
                    local = observe_local_binding(binding, profile=profile, now=now)
                    # An unreadable local authority must not fall back to a stale
                    # own replica, even one which previously proved completion.
                    current = local is not None
                    released = False
                    if local is not None:
                        task = local
                        released = _owner_evidence(task).get('verified') is True
                status = task.get('status')
                # Acceptance alone is not a release: an initially terminal publication releases only with
                # verified owner evidence (checked above). Unknown status also remains unfinished.
                known = status in ('todo', 'ready', 'running', 'scheduled', 'review',
                                   'blocked', 'triage', 'done', 'archived')
                # [daveey] A currently observed owner archive without a verified completion produces nothing: the
                # card is closed, not completed. It neither waits nor counts as done, and never releases as done.
                closed = bool(current and status == 'archived' and not released and _owner_evidence(task))
                if not current or not known or (status in ('done', 'archived') and not released and not closed):
                    status = 'unknown'
                from hermes_cli.kanban_completion_receipt_projection import remote_fields
                result.append({
                    **remote_fields(task, **owner),
                    **{k: task.get(k) for k in ('title', 'assignee', 'block_kind', 'created_at', 'started_at', 'priority')},
                    **owner, 'id': owner['task_id'],
                    'key': '/'.join(owner[k] for k in ('gateway', 'profile', 'board', 'task_id')),
                    'session': resolved, 'origin_session': origin['session'],
                    'origin_profile': origin['profile'], 'origin_gateway': origin['gateway'],
                    'binding_id': binding['binding_id'], 'observation_version': version,
                    'status': status, 'reported_status': task.get('status'),
                    'unfinished': not (current and (released or closed) and status in ('done', 'archived')),
                    **({'closed_uncompleted': True} if closed else {}),
                    'stale': not current, 'synced_at': last_ok,
                    'remote': True, 'via': 'origin',
                    'legacy_aliases': aliases_for_projection(alias_receipts.get(binding_hash(binding), []), binding),
                    # [daveey] Bounded run fields for what the card waits on (kanban_wait_info, display only).
                    'park': _park_of(task),
                    **{k: task[k] for k in ('awaiting', 'wake_check', 'human_request') if task.get(k)},
                })
        return result
    finally:
        if source is not None:
            source.close()


def accept_binding(*, path: Path, replica: Path, binding: dict, request_id: str,
                   sender: str, gateway: str, profile: str,
                   resolve_session: Callable[[str], str], legacy_alias: dict | None = None) -> dict:
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
        raise ValueError("origin_request_invalid")
    digest = binding_hash(binding)
    from hermes_cli import kanban_origin_legacy as legacy
    if legacy_alias is not None:
        legacy.validate_alias(legacy_alias, binding)
    if (sender != f"gw:{binding['owner']['gateway']}"
            or gateway != binding['origin']['gateway'] or profile != binding['origin']['profile']):
        raise ValueError("origin_route_mismatch")
    prior = receipt(path, request_id)
    if prior:
        if prior["binding_hash"] != digest or prior.get('legacy_alias') != legacy_alias:
            raise ValueError("origin_binding_conflict")
        return prior
    if not replica.is_file():
        return {"status": "pending", "code": "origin_publication_pending"}
    from hermes_cli.kanban_origin import _ro
    db = _ro(replica)
    try:
        db.execute('BEGIN')
        row = db.execute("SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                         (binding["owner"]["gateway"], binding["task_record_key"])).fetchone()
        if legacy_alias is not None and not legacy.verify_publication(db, legacy_alias, binding):
            return {"status": "pending", "code": "origin_publication_pending"}
    finally:
        db.close()
    if not row or row[0] < binding["publication_version"]:
        return {"status": "pending", "code": "origin_publication_pending"}
    task = json.loads(row[1])
    owner = binding["owner"]
    if any(task.get(field) != owner[field] for field in ("gateway", "profile", "board")) or task.get("id") != owner["task_id"]:
        raise ValueError("origin_publication_mismatch")
    # The immutable marker is published BEFORE the server assigns a record
    # version. Only the courier envelope adds that publication lower bound.
    marker = {k: v for k, v in binding.items() if k != "publication_version"}
    markers = [{k: v for k, v in m.items() if k != "publication_version"}
               for m in task.get("origin_bindings", []) if isinstance(m, dict)]
    if marker not in markers:
        return {"status": "pending", "code": "origin_publication_pending"}
    resolved = resolve_session(binding["origin"]["session"])
    if not isinstance(resolved, str) or not resolved:
        raise ValueError("origin_session_missing")
    ack = {"status": "accepted", "request_id": request_id, "binding_id": binding["binding_id"],
           "binding_hash": digest, "origin": binding["origin"], "owner": binding["owner"],
           "resolved_session": resolved, "publication_version": row[0]}
    if legacy_alias is not None:
        ack['legacy_alias'] = legacy_alias
    with _db(path) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _read_receipt(conn, request_id)
        if existing:
            if existing["binding_hash"] != digest or existing.get('legacy_alias') != legacy_alias:
                raise ValueError("origin_binding_conflict")
            return existing
        if legacy_alias is not None:
            legacy.check_conflicts(conn, legacy_alias, binding)
        existing = conn.execute("SELECT binding_hash,resolved_session,observation_version FROM origin_obligations WHERE binding_id=?", (_binding_key(binding),)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("origin_binding_conflict")
            # An immutable binding retry acknowledges what is actually durable,
            # not a newer resolution that this transaction did not store.
            ack["resolved_session"], ack["publication_version"] = existing[1], existing[2]
        # [daveey] An obligation accepted after its owner task already finished (terminal + verified) is released
        # at write time: no newer replica version will ever arrive for refresh_observations to advance it.
        conn.execute("INSERT INTO origin_obligations (binding_id,binding_hash,binding_json,resolved_session,task_json,observation_version,released) VALUES (?,?,?,?,?,?,?) ON CONFLICT(binding_id) DO NOTHING",
                     (_binding_key(binding), digest, _json(binding), resolved, _json(task), row[0],
                      int(_verified_terminal(task))))
        conn.execute("INSERT INTO origin_receipts VALUES (?,?,?)", (request_id, digest, _json(ack)))
    readback = receipt(path, request_id)
    assert readback is not None
    return readback
