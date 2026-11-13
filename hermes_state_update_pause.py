"""One-shot, store-only update checkpoints. Status stays started for old binaries.

A receipt proves a persisted completed-tool boundary, not process quiescence. The
owner must stop its in-process executor before publishing parked accounting. No
worker execution may be live or uncertain. Resuming consumes proof BEFORE running.
"""
import hashlib
import json

from hermes_state_runtime import (
    RuntimeStoreError, _admission, _canonical_chain, _epoch, _json, _row, _session, _text,
)

CHECKPOINT_PREFIX = 'gateway.update_checkpoint.v1.'
MAX_SUFFIX_ROWS = 129  # one assistant plus at most 128 results
MAX_SUFFIX_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 8192


def _refuse(detail):
    raise RuntimeStoreError('invalid_checkpoint', field='update_checkpoint', detail=detail)


def _binding(conn, row, physical_session_id, transcript_mark):
    """Recomputed on the transaction connection; nothing trusts a caller's history."""
    if row['status'] != 'started' or type(row['generation']) is not int:
        _refuse('admission is not started')
    logical = row['target_session_id']
    session = _session(conn, logical)
    if session['runtime_generation'] != row['generation']:
        _refuse('generation changed')
    from hermes_state_local_lineage import local_physical_target
    target = local_physical_target(conn, logical)
    chain = _canonical_chain(conn, target)
    if physical_session_id != chain[-1]:
        _refuse('physical tip changed')
    physical = _session(conn, physical_session_id)
    if physical['runtime_generation'] > row['generation']:
        _refuse('physical generation is ahead')
    if physical['ended_at'] is not None or session['archived'] or physical['archived']:
        _refuse('session is closed')
    from hermes_state_runtime_workers import worker_states
    if worker_states(conn, logical):
        _refuse('worker execution is live or uncertain')
    if conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND status='terminal' "
                    "AND outcome='steering' AND owner_epoch=? AND generation=? LIMIT 1",
                    (logical, row['owner_epoch'], row['generation'])).fetchone():
        _refuse('steering transfer exists')
    if conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? "
                    "AND status IN ('started','unknown') AND admission_id!=? LIMIT 1",
                    (logical, row['admission_id'])).fetchone():
        _refuse('competing execution exists')
    if type(transcript_mark) is not int or transcript_mark < 0:
        _refuse('invalid transcript mark')
    # Probe metadata first: a huge message cannot force an unbounded Python read.
    columns = [r[1] for r in conn.execute('PRAGMA table_info(messages)')]
    sizes = '+'.join(f'COALESCE(length(CAST("{c}" AS BLOB)),0)' for c in columns)
    tail = conn.execute(f'SELECT id,role,({sizes}) AS size FROM messages WHERE session_id=? '
                        'ORDER BY id DESC LIMIT ?', (physical_session_id, MAX_SUFFIX_ROWS + 1)).fetchall()
    suffix = []
    for item in tail:
        suffix.append(item)
        if item['role'] != 'tool':
            break
    if (len(suffix) < 2 or len(suffix) > MAX_SUFFIX_ROWS or suffix[-1]['role'] != 'assistant'
            or any(r['role'] != 'tool' for r in suffix[:-1])):
        _refuse('not a completed tool suffix')
    if suffix[-1]['id'] <= transcript_mark:
        _refuse('boundary predates claim')
    if sum(r['size'] for r in suffix) > MAX_SUFFIX_BYTES:
        _refuse('tool suffix too large')
    messages = [dict(r) for r in conn.execute('SELECT * FROM messages WHERE session_id=? AND id>=? ORDER BY id',
                                              (physical_session_id, suffix[-1]['id']))]
    if any(m['active'] != 1 for m in messages):
        _refuse('tool suffix is inactive')
    try:
        calls = json.loads(messages[0]['tool_calls'])
        ids = [c['id'] for c in calls]
        results = [r['tool_call_id'] for r in messages[1:]]
        if (not isinstance(calls, list) or not ids or any(not isinstance(i, str) or not i for i in ids)
                or len(ids) != len(set(ids)) or len(results) != len(set(results)) or set(ids) != set(results)):
            _refuse('tool calls and results differ')
    except (TypeError, KeyError, ValueError) as exc:
        if isinstance(exc, RuntimeStoreError):
            raise
        _refuse('malformed tool calls')
    fingerprint = [{k: {'blob': v.hex()} if isinstance(v, bytes) else v for k, v in m.items()}
                   for m in messages]
    return dict(version=1, admission_id=row['admission_id'], owner_epoch=row['owner_epoch'],
                generation=row['generation'], session_id=logical, physical_session_id=physical_session_id,
                lineage_json=row['lineage_json'], physical_chain=chain, payload_digest=row['payload_digest'],
                physical_generation=physical['runtime_generation'],
                transcript_mark=transcript_mark, max_message_id=suffix[0]['id'],
                suffix_start_id=suffix[-1]['id'],
                suffix_digest=hashlib.sha256(_json({'messages': fingerprint}).encode('utf-8')).hexdigest())


def _valid_checkpoint(conn, row):
    saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                         (CHECKPOINT_PREFIX + row['admission_id'],)).fetchone()
    if saved is None or not isinstance(saved[0], str) or len(saved[0]) > MAX_RECEIPT_BYTES:
        return None
    try:
        receipt = json.loads(saved[0])
        if not isinstance(receipt, dict):
            return None
        actual = _binding(conn, row, receipt['physical_session_id'], receipt['transcript_mark'])
        # JSON equality alone accepts bool as int. Canonical bytes also bind types.
        return actual if _json(actual) == _json(receipt) else None
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def parked_update_turn(conn, session_ids):
    """[daveey] t_01a1d6f94ac774a2: the admission id of a started turn parked by an update checkpoint
    whose conversation covers any of ``session_ids``, else None.

    A receipt proves its turn by "no row after the tool suffix", so a row a passive writer (a Kanban or
    completion notice) appends while the turn is parked voids it and the turn is dropped on resume. A
    parked turn holds no turn lease, so passive writers ask this instead. Receipt validity is not judged
    here: an unreadable receipt still fences its logical target until resume or recovery settles it."""
    wanted = {sid for sid in session_ids if sid}
    end = CHECKPOINT_PREFIX[:-1] + chr(ord(CHECKPOINT_PREFIX[-1]) + 1)
    for key, value in conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<?',
                                   (CHECKPOINT_PREFIX, end)).fetchall():
        admission_id = key[len(CHECKPOINT_PREFIX):]
        row = conn.execute("SELECT target_session_id FROM session_admissions WHERE admission_id=? "
                           "AND status='started'", (admission_id,)).fetchone()
        if row is None:
            continue
        bound = {row[0]}
        try:
            receipt = json.loads(value) if isinstance(value, str) and len(value) <= MAX_RECEIPT_BYTES else None
        except (ValueError, RecursionError):
            receipt = None
        if isinstance(receipt, dict):
            chain = receipt.get('physical_chain')
            bound.update(s for s in [receipt.get('physical_session_id'), *(chain if isinstance(chain, list) else ())]
                         if isinstance(s, str))
        if wanted & bound:
            return admission_id
    return None


def refuse_parked_write(conn, session_id, conversation_id):
    """A passive (lease-less) transcript write's in-transaction guard: the same retryable "busy" refusal a
    live turn lease gives, so callers rewind and retry after the parked turn resumes or is settled."""
    from hermes_state_errors import SessionTurnLeaseLostError
    if parked_update_turn(conn, (session_id, conversation_id)) is not None:
        raise SessionTurnLeaseLostError(
            f'Session has a turn parked for an update; refusing transcript mutation for {session_id!r}')


def delete_session_checkpoint(conn, admission_id):
    """Same-transaction cleanup; never deletes unrelated state_meta receipts."""
    conn.execute('DELETE FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + admission_id,))


def _turn_persisted(conn, row, transcript_mark):
    """The claim's own user message is in the transcript the continuation resumes: a user row of the
    conversation's chain written after the claim's ``transcript_mark``."""
    if type(transcript_mark) is not int or transcript_mark < 0:
        _refuse('invalid transcript mark')
    from hermes_state_local_lineage import local_physical_target
    logical = row['target_session_id']
    chain = sorted({logical, *_canonical_chain(conn, local_physical_target(conn, logical))})
    marks = ','.join('?' * len(chain))
    return conn.execute(f"SELECT 1 FROM messages WHERE session_id IN ({marks}) AND id>? AND role='user' LIMIT 1",
                        (*chain, transcript_mark)).fetchone() is not None


def _settle_folded(conn, row, folded, transcript_mark):
    """[daveey] t_01a1d6d8bf621f17: rows folded into the claim's text (``session_fold_pending``) settle with
    the checkpoint, in its write. The paused turn's user message carries their text and the continuation
    resumes that persisted history, so once it is persisted each reads delivered (``steered``, acked once);
    re-queued, the continuation would fold it again and the model would read it twice. Only when nothing of
    the turn was persisted does each go back to its own FIFO place."""
    from hermes_state_runtime import STEERING, _requeue_transfer
    epoch, generation = row['owner_epoch'], row['generation']
    durable = _turn_persisted(conn, row, transcript_mark)
    for admission_id in folded:
        if not durable:
            _requeue_transfer(conn, admission_id=admission_id, epoch=epoch, generation=generation)
            continue
        conn.execute("UPDATE session_admissions SET outcome='steered' WHERE admission_id=? AND status='terminal' "
                     "AND outcome=? AND owner_epoch=? AND generation=?",
                     (admission_id, STEERING, epoch, generation))


def checkpoint_session_input(db, *, epoch, admission_id, generation, physical_session_id, transcript_mark,
                             folded=()):
    """Park an exact started generation at a fresh complete persisted tool suffix.

    transcript_mark is the claim's global messages.id high-water mark. The receipt
    is bounded and immutable: retries must prove the exact same boundary. ``folded`` are the
    steering transfers folded into the claim's own text; they settle in this write (``_settle_folded``).
    """
    _text(admission_id)
    _text(physical_session_id)
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        if row['owner_epoch'] != epoch or type(generation) is not int or row['generation'] != generation:
            raise RuntimeStoreError('stale_generation')
        if folded:
            _settle_folded(conn, row, folded, transcript_mark)
        receipt = _binding(conn, row, physical_session_id, transcript_mark)
        encoded = _json(receipt)
        if len(encoded.encode('utf-8')) > MAX_RECEIPT_BYTES:
            _refuse('receipt too large')
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + admission_id,)).fetchone()
        if old is not None and old[0] != encoded:
            _refuse('checkpoint already differs')
        conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)',
                     (CHECKPOINT_PREFIX + admission_id, encoded))
        return receipt
    return db._execute_write(write)


def get_session_checkpoint(db, *, session_id):
    """Valid proof for the exact logical FIFO, else None; never repairs receipts."""
    with db._read_ctx() as conn:
        rows = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? AND status='started'",
                            (session_id,)).fetchall()
        return _valid_checkpoint(conn, rows[0]) if len(rows) == 1 else None


def resume_session_checkpoint(db, *, epoch, admission_id, _guard=None):
    """Atomically fence the old attempt, advance generation and consume its proof.

    Returns the original admission row with update_resume=receipt. Caller continues
    persisted history, never resubmits payload. _guard is the owner's admission gate.
    """
    def write(conn):
        _epoch(conn, epoch)
        if _guard is not None:
            _guard(conn)
        row = _admission(conn, admission_id)
        receipt = _valid_checkpoint(conn, row)
        if receipt is None:
            _refuse('missing or invalid checkpoint')
        generation = row['generation'] + 1
        targets = {row['target_session_id'], receipt['physical_session_id']}
        for target in targets:
            conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (generation, target))
        conn.execute('UPDATE session_admissions SET owner_epoch=?,generation=? WHERE admission_id=?',
                     (epoch, generation, admission_id))
        delete_session_checkpoint(conn, admission_id)
        result = _row(_admission(conn, admission_id))
        result['update_resume'] = receipt
        result['transcript_mark'] = conn.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
        return result
    return db._execute_write(write)


def requeue_unstarted_input(db, *, epoch, admission_id, generation, transcript_mark, transfers=None):
    """[daveey] t_01a1d49d50e4c656: hand an exact started generation that never began its turn back to its FIFO place.

    The claim held no proof of a boundary because the turn wrote nothing: refused (``invalid_checkpoint``) once any
    transcript row of the conversation's chain postdates the claim's ``transcript_mark``, or once a worker, a
    steering transfer or a checkpoint exists for it. ``seq`` is untouched, so the row is the FIFO head again and
    the next claim re-validates it (a held notifier wake then waits queued). The old generation is fenced: its
    status is no longer started, so its settlement and checkpoint writes refuse.

    [daveey] t_01a1d4d4c30c7e43: ``transfers`` (``{admission_id: delivered}``, the owner's ``steer_transfers`` read
    after the execution returned) hands back, in this write, every steering transfer bound to the generation that
    no drain of the turn took (``_requeue_transfer``, its own FIFO place): a steer committed while the turn waited
    on the session turn lease must not cost the wake its re-queue. Refused (``steering transfer delivered``) when
    one reached the turn; without ``transfers`` any transfer refuses, as before."""
    _text(admission_id)
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        logical = row['target_session_id']
        if (row['status'] != 'started' or row['owner_epoch'] != epoch or type(generation) is not int
                or row['generation'] != generation or _session(conn, logical)['runtime_generation'] != generation):
            raise RuntimeStoreError('stale_generation')
        if type(transcript_mark) is not int or transcript_mark < 0:
            _refuse('invalid transcript mark')
        from hermes_state_runtime_workers import worker_states
        if worker_states(conn, logical):
            _refuse('worker execution is live or uncertain')
        steering = [r[0] for r in conn.execute(
            "SELECT admission_id FROM session_admissions WHERE target_session_id=? AND status='terminal' "
            "AND outcome='steering' AND owner_epoch=? AND generation=? ORDER BY seq", (logical, epoch, generation))]
        if steering and transfers is None:
            _refuse('steering transfer exists')
        if any((transfers or {}).get(steer) for steer in steering):
            _refuse('steering transfer delivered')
        if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + admission_id,)).fetchone():
            _refuse('checkpoint exists')
        from hermes_state_local_lineage import local_physical_target
        chain = sorted({logical, *_canonical_chain(conn, local_physical_target(conn, logical))})
        marks = ','.join('?' * len(chain))
        if conn.execute(f'SELECT 1 FROM messages WHERE session_id IN ({marks}) AND id>? LIMIT 1',
                        (*chain, transcript_mark)).fetchone():
            _refuse('turn wrote its transcript')
        conn.execute("UPDATE session_admissions SET status='queued',generation=NULL WHERE admission_id=? "
                     "AND status='started'", (admission_id,))
        from hermes_state_runtime import _requeue_transfer
        for steer in steering:
            _requeue_transfer(conn, admission_id=steer, epoch=epoch, generation=generation)
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?', (logical,))
        return _row(_admission(conn, admission_id))
    return db._execute_write(write)


def recover_checkpointed_inputs(conn, epoch):
    """Old started attempts without exact proof become unknown; return their count."""
    changed = 0
    for row in conn.execute("SELECT * FROM session_admissions WHERE status='started' AND owner_epoch!=?",
                            (epoch,)).fetchall():
        if _valid_checkpoint(conn, row) is not None:
            continue
        delete_session_checkpoint(conn, row['admission_id'])
        conn.execute("UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (row['admission_id'],))
        changed += 1
    return changed
