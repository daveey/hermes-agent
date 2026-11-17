"""Real FIFO claims must leave the owner loop free during every pending read."""
import asyncio
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_claim_pending_read_keeps_the_owner_loop_ticking(tmp_path, monkeypatch):
    import hermes_state_runtime as rt
    from gateway import session_authority
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False, adapters={}), profile_id='p', instance_id='t',
                                 db=db, epoch=rt.begin_runtime_epoch(db, instance_id='t'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='u'), 'route')
    row = rt.admit_session_input(db, epoch=authority.epoch, principal_id='u', session_id='s',
                                 request_id='r1', payload={'text': 'x'})
    real = rt.list_session_admissions
    loop, owner = asyncio.get_running_loop(), threading.get_ident()
    reads, publications = [], []

    def slow_list(*args, **kwargs):
        progressed = threading.Event()

        def tick():
            # Stream readers must also progress: moving I/O to a worker while it
            # holds this lock would still block every loop-side stream reader.
            with live.event_stream.lock:
                progressed.set()

        loop.call_soon_threadsafe(tick)
        reads.append((threading.get_ident() != owner, progressed.wait(2)))
        return real(*args, **kwargs)

    monkeypatch.setattr(session_authority, 'list_session_admissions', slow_list)
    publish = live.event_stream.publish

    def observe(*args, **kwargs):
        publications.append((threading.get_ident(), kwargs.get('event_type')))
        return publish(*args, **kwargs)

    monkeypatch.setattr(live.event_stream, 'publish', observe)
    try:
        claimed, first = await asyncio.wait_for(authority._claim_next(SessionRef('p', 's'), live), 30)
        assert first['admission_id'] == row['admission_id']
        assert claimed and claimed['admission_id'] == row['admission_id'] and claimed['status'] == 'started'
        assert len(reads) >= 2, 'exercise both preclaim selection and postclaim publication reads'
        assert all(off_loop and progressed for off_loop, progressed in reads), reads
        assert publications == [(owner, 'message.start'), (owner, 'session.info')]
        assert live.event_stream.execution['admission_id'] == row['admission_id']
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_observer', [False, True])
@pytest.mark.parametrize('frozen_source', [None, 'cron'])
@pytest.mark.parametrize('invalidation', ['publication', 'settling', 'epoch', 'source', 'transfer'])
async def test_claim_publication_rechecks_a_newer_view(
        tmp_path, monkeypatch, cancel_observer, frozen_source, invalidation):
    import hermes_state_runtime as rt
    from gateway import session_claim_publication as publication
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cli')
    authority = SessionAuthority(SimpleNamespace(_draining=False, adapters={}), profile_id='p', instance_id='t',
                                 db=db, epoch=rt.begin_runtime_epoch(db, instance_id='t'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='u'), 'route')
    ref = SessionRef('p', 's')
    if frozen_source is not None:
        authority.launch_sources['s'] = frozen_source
    first = rt.admit_session_input(db, epoch=authority.epoch, principal_id='u', session_id='s',
                                   request_id='r1', payload={'text': 'first'})
    ready, release = asyncio.Event(), threading.Event()
    loop, prepare, attempts, infos = asyncio.get_running_loop(), publication._prepare, [], []

    def held_snapshot(*args):
        result = prepare(*args)
        attempts.append(result)
        if len(attempts) == 1:
            loop.call_soon_threadsafe(ready.set)
            assert release.wait(10), 'test must release its owned snapshot worker'
        return result

    monkeypatch.setattr(publication, '_prepare', held_snapshot)
    publish = live.event_stream.publish

    def observe(session_id, payload, *, event_type):
        if event_type == 'session.info':
            infos.append(payload)
        return publish(session_id, payload, event_type=event_type)

    monkeypatch.setattr(live.event_stream, 'publish', observe)
    task = asyncio.create_task(authority._claim_next(ref, live))
    try:
        await asyncio.wait_for(ready.wait(), 10)
        # A committed claim is already stamped even while its view waits on I/O.
        assert live.event_stream.execution['admission_id'] == first['admission_id']
        second = rt.admit_session_input(db, epoch=authority.epoch, principal_id='u', session_id='s',
                                        request_id='r2', payload={'text': 'second'})
        generation = authority.executing['s']
        if invalidation == 'transfer':
            rt.steer_session_input(db, epoch=authority.epoch, admission_id=second['admission_id'],
                                   generation=generation)
        if invalidation == 'source':
            frozen_source = 'cli' if frozen_source == 'cron' else 'cron'
        changes = {
            'transfer': lambda: authority.steer_transfers.update(
                s=(generation, {second['admission_id']: False})),
            'publication': lambda: authority._publish_pending(ref),
            # A committed settlement remains visible as started until published.
            'settling': lambda: live.settling.update({second['admission_id']: dict(second, status='started')}),
            # Same sequence in a different replay lifetime must not validate.
            'epoch': lambda: setattr(live.event_stream, 'epoch', 'replacement-replay-epoch'),
            'source': lambda: authority.launch_sources.update(s=frozen_source),
        }
        with live.event_stream.lock:
            changes[invalidation]()
        if cancel_observer:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        if not cancel_observer:
            claimed, selected = await asyncio.wait_for(task, 10)
            assert claimed['admission_id'] == selected['admission_id'] == first['admission_id']
        await asyncio.wait_for(asyncio.gather(*mutation_tasks(authority)), 10)
        assert [r['admission_id'] for r in infos[-1]['pending']] == [first['admission_id'], second['admission_id']]
        assert [r['status'] for r in infos[-1]['pending']] == [
            'started', 'started' if invalidation in ('settling', 'transfer') else 'queued']
        assert infos[-1]['pending'][1]['steering'] is (invalidation == 'transfer')
        assert infos[-1]['pending'][1]['controls'] == (
            [] if frozen_source == 'cron' or invalidation in ('settling', 'transfer') else ['steer', 'replace'])
        assert infos[-1]['running'] is True
        assert len(attempts) == 2, 'discard the stale view rather than overwriting a newer publication'
    finally:
        release.set()
        await asyncio.gather(task, *mutation_tasks(authority), return_exceptions=True)
        db.close()
