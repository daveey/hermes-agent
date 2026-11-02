"""A following Bot Chat's selection reaches the agent build on every entry, through real admissions. [daveey]

A desktop Bot Chat is not managed (``managed_policy`` is None): its turns build in process. The owner
commits the turn's effective selection once, from the one profile read the turn runs, under its
transaction (owner epoch, logical generation, receipt policy), and records the switch intent for the
session's PHYSICAL transcript (``local_physical_target``), which is the id both the in-process build and
``reserve_admission_worker``'s scope use after compression.

Real: SessionAuthority + AuthorityConnection, ``authority.submit`` -> the FIFO drain ->
``session_ingress.execute_admission``; for the managed path ``admit_session_input`` /
``claim_session_input`` / ``reserve_admission_worker`` with a live child process, then the owner's
``_bootstrap`` and ``managed_worker.execute``; real ``AIAgent`` / ``prepare_agent_route`` / durable
``RoutingState``; real ``session.mutate``. Seam: ``runner._handle_message`` stands in for the TurnRunner,
doing what ``run_sync`` does before its agent exists (``policy_scope``, the policy route, ``current_model``,
``AIAgent`` on the turn's physical session) on an executor thread that copies the context.
"""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from tests.gateway.test_model_switch_worker_composed import (
    home, OPUS, ASTRA, _owner, _allocation, _live_policy, _mutate, PERSIST_FIELDS,
)
from tests.gateway.test_bot_chat_follows_profile import _open
from tests.gateway.test_bot_chat_managed_follow import _profile_pick, _receipt_model
# autouse: the owner's state.db lives in its profile home, beside the config.yaml profile_config reads
from tests.gateway.test_bot_chat_managed_follow import _state_db_in_profile
from tests.waiting import async_wait_until


def _in_process_handler(built):
    """``runner._handle_message`` doing what ``TurnRunner.run_sync`` does up to its agent build."""
    state = {}

    def build(authority, ref):
        from gateway.session_policy import policy_for_source, policy_scope
        from run_agent import AIAgent
        runner, live = authority.runner, authority.sessions[ref.session_id]
        policy = policy_for_source(runner, live.source)
        with policy_scope(policy, authority=authority):
            model, runtime = runner._resolve_session_agent_runtime(source=live.source, session_key=live.route)
            model = policy.current_model(authority)
            try:
                agent = AIAgent(model=model, provider=runtime.get('provider'), session_id=authority.physical_target(ref),
                                session_db=authority.db, quiet_mode=True, skip_memory=True, skip_context_files=True)
            except Exception as exc:
                return ('raised', f'{model}/{runtime.get("provider")}', f'{type(exc).__name__}: {exc}')
        return agent.model, agent.provider, agent.session_id

    async def handle(event):
        authority, ref = state['authority'], state['ref']
        try:
            built.append(await asyncio.to_thread(build, authority, ref))
        except Exception as exc:  # the test asserts on what a turn would have raised
            built.append(('raised', type(exc).__name__, str(exc)))
        return 'ok'
    return handle, state


async def _turn(authority, owner, ref, built, rid):
    from gateway.session_contract import Submission
    count = len(built)
    await authority.submit(owner.actor, Submission(rid, ref, {'text': rid}, 'queue'))
    await async_wait_until(lambda: len(built) > count, timeout=30, what=f'turn {rid}')
    return built[-1]


def _bot_chat_owner(home, monkeypatch):
    built = []
    handle, state = _in_process_handler(built)
    store, db, authority, owner, ref = _owner(home, monkeypatch, handle)
    authority.runner._peek_session_state = lambda key: None
    authority.runner.session_authority = authority  # what active_authority(runner) answers in production
    state.update(authority=authority, ref=ref)
    db.set_session_title(ref.session_id, 'Bot Chat')
    return store, db, authority, owner, ref, built


def _compress(db, authority, ref, suffix):
    """A rotating compaction of the session's current transcript (what the agent's compressor publishes)."""
    from agent.model_routing_state import RoutingState
    from hermes_constants import get_hermes_home
    parent = authority.physical_target(ref)
    child = f'{ref.session_id}-{suffix}'
    db.append_message(parent, 'user', 'before ' + suffix)
    RoutingState(get_hermes_home() / 'model-routing.sqlite3').link_continuation(child, parent)
    db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='local',
                                 messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
    route = authority.sessions[ref.session_id].route
    assert authority.runner.session_store.advance_compression_session(route, parent, child) is not None
    assert authority.physical_target(ref) == child
    return child


@pytest.mark.asyncio
@pytest.mark.parametrize('compressed', [False, True], ids=['flat', 'compressed'])
async def test_in_process_bot_chat_follows_profile_across_exact_pin(home, monkeypatch, compressed):
    """The production Desktop path: an unmanaged Bot Chat's next in-process build follows the profile
    off an exact pin, on its physical transcript, and back again."""
    from gateway.session_managed_worker import managed_policy
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        assert managed_policy(authority, ref) is None
        assert (await _turn(authority, owner, ref, built, 'opus'))[:2] == (OPUS, 'anthropic'), built
        assert _allocation(home, ref.session_id)['exact_pin'] is True
        if compressed:
            _compress(db, authority, ref, 'tip')
        physical = authority.physical_target(ref)
        _profile_pick(home, ASTRA, 'openai-codex')
        assert await _turn(authority, owner, ref, built, 'astra') == (ASTRA, 'openai-codex', physical), built
        assert _allocation(home, physical)['selected_model'] == ASTRA
        _profile_pick(home, OPUS, 'anthropic')
        assert (await _turn(authority, owner, ref, built, 'back'))[:2] == (OPUS, 'anthropic'), built
        # An unchanged selection re-arms nothing: the consumed intent stays consumed.
        assert (await _turn(authority, owner, ref, built, 'same'))[:2] == (OPUS, 'anthropic'), built
        from agent.model_routing_switch_intent import PREFIX
        assert db.get_meta(PREFIX + physical) is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_in_process_turn_reads_the_profile_once(home, monkeypatch):
    """The owner's commit and the turn's route come from one profile read: a profile edit landing
    between them cannot pair the committed selection with another build."""
    import gateway.session_policy as sp
    store, _db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        await _turn(authority, owner, ref, built, 'opus')
        _profile_pick(home, ASTRA, 'openai-codex')
        reads = []
        real = sp.profile_config

        def counted(authority):
            reads.append(1)
            config = real(authority)
            if len(reads) == 1:  # the profile moves right after the turn's read
                _profile_pick(home, OPUS, 'anthropic')
            return config
        monkeypatch.setattr(sp, 'profile_config', counted)
        assert (await _turn(authority, owner, ref, built, 'astra'))[:2] == (ASTRA, 'openai-codex'), built
        assert len(reads) == 1, reads
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_in_process_composer_hold_until_next_profile_edit_then_follows(home, monkeypatch):
    """A composer pick holds against the profile target it was made on, ends durably on the next
    profile edit, and the turn after that follows."""
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        db.patch_session_model_config(ref.session_id, {
            'composer_override_profile': {'model': ASTRA, 'provider': 'openai-codex'}})
        _profile_pick(home, ASTRA, 'openai-codex')
        await _open(authority, ref)
        assert (await _turn(authority, owner, ref, built, 'held'))[:2] == (OPUS, 'anthropic'), built
        _profile_pick(home, OPUS, 'anthropic')
        assert (await _turn(authority, owner, ref, built, 'ended'))[:2] == (OPUS, 'anthropic'), built
        assert 'selection_profile' not in _receipt_model(db, ref)
        _profile_pick(home, ASTRA, 'openai-codex')
        assert (await _turn(authority, owner, ref, built, 'follows'))[:2] == (ASTRA, 'openai-codex'), built
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_follow_after_explicit_pick_consumed_by_another_build(home, monkeypatch):
    """An explicit pick's intent consumed by a same-session build outside the turn (a cold ``/review``
    parent) must not leave the follow selection's bookkeeping claiming the allocation it replaced."""
    from run_agent import AIAgent
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        _profile_pick(home, ASTRA, 'openai-codex')
        assert (await _turn(authority, owner, ref, built, 'astra'))[:2] == (ASTRA, 'openai-codex'), built
        # The profile moves with no turn after it; the user then picks that same target explicitly.
        _profile_pick(home, OPUS, 'anthropic')
        reply = await _mutate(owner, db, ref, 'pick', {'model': OPUS, 'provider': 'anthropic'})
        assert reply['result']['model'] == OPUS, reply
        # The cold-review parent builds on the session's next-turn route and consumes the pick's intent.
        AIAgent(model=OPUS, provider='anthropic', session_id=authority.physical_target(ref), session_db=db,
                quiet_mode=True, skip_memory=True, skip_context_files=True)
        assert _allocation(home, ref.session_id)['selected_model'] == OPUS
        # The next profile edit ends the pick on the tuple the follow selection last committed (Astra).
        _profile_pick(home, ASTRA, 'openai-codex')
        assert (await _turn(authority, owner, ref, built, 'follows'))[:2] == (ASTRA, 'openai-codex'), built
        assert _allocation(home, ref.session_id)['selected_model'] == ASTRA
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_class_profile_default_follows_off_exact_pin(home, monkeypatch):
    store, _db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        assert (await _turn(authority, owner, ref, built, 'opus'))[:2] == (OPUS, 'anthropic'), built
        from hermes_cli.config import atomic_config_write, read_user_config_raw
        config = read_user_config_raw(home / 'config.yaml')
        config['model'].update(default='class:deep', provider='auto')
        atomic_config_write(home / 'config.yaml', config)
        result = await _turn(authority, owner, ref, built, 'class')
        assert result[0] != 'raised', result
        recorded = _allocation(home, ref.session_id)
        assert not recorded.get('exact_pin', False), recorded
        assert recorded['selected_model'] == result[0]
    finally:
        await owner.close()
        store.close_all_db_handles()


def _class_default(home):
    from hermes_cli.config import atomic_config_write, read_user_config_raw
    config = read_user_config_raw(home / 'config.yaml')
    config['model'].update(default='class:deep', provider='auto')
    atomic_config_write(home / 'config.yaml', config)


def _routing_ledger(home, sid):
    """What a reroll would change: the lineage's allocation, the allocator's counters, its honoured fences."""
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(home / 'model-routing.sqlite3')) as db:
        counters = db.execute('SELECT COALESCE(SUM(position), 0) FROM counters').fetchone()[0]
        fences = sorted(db.execute('SELECT root, class, intent FROM switch_fences').fetchall())
    return _allocation(home, sid), counters, fences


def _forget_selection(db, ref):
    """A session no follow-selection writer committed yet (first run after upgrade)."""
    from gateway.session_profile_selection import PREFIX
    db._execute_write(lambda c: c.execute('DELETE FROM state_meta WHERE key=?', (PREFIX + ref.session_id,)))


@pytest.mark.asyncio
@pytest.mark.parametrize('event', ['compaction', 'missing_memo', 'both'])
async def test_unchanged_class_selection_never_rerolls(home, monkeypatch, event):
    """No profile edit, no pick: a compaction (new physical target) or a missing selection memo is not a
    selection change, so the class allocation keeps its provider and nothing is allocated again."""
    from agent.model_routing_switch_intent import PREFIX as INTENT
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        await _turn(authority, owner, ref, built, 'opus')
        _class_default(home)
        await _turn(authority, owner, ref, built, 'c1')
        before = await _turn(authority, owner, ref, built, 'c2')
        ledger = _routing_ledger(home, authority.physical_target(ref))
        if event in ('compaction', 'both'):
            _compress(db, authority, ref, 'tip')
        if event in ('missing_memo', 'both'):
            _forget_selection(db, ref)
        after = await _turn(authority, owner, ref, built, 'c3')
        physical = authority.physical_target(ref)
        assert after[:2] == before[:2], built
        assert _routing_ledger(home, physical) == ledger
        assert db.get_meta(INTENT + physical) is None
        # Once re-baselined, a real profile edit still switches.
        _profile_pick(home, ASTRA, 'openai-codex')
        assert await _turn(authority, owner, ref, built, 'astra') == (ASTRA, 'openai-codex', physical), built
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_missing_memo_class_selection_still_corrects_an_exact_pin(home, monkeypatch):
    """First run after upgrade with the profile on a class and the lineage still exactly pinned: the class
    selection must move the allocation off the pin (the change the session never committed)."""
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        assert (await _turn(authority, owner, ref, built, 'opus'))[:2] == (OPUS, 'anthropic'), built
        assert _allocation(home, ref.session_id)['exact_pin'] is True
        _forget_selection(db, ref)
        _class_default(home)
        result = await _turn(authority, owner, ref, built, 'class')
        assert result[0] != 'raised', result
        recorded = _allocation(home, ref.session_id)
        assert not recorded.get('exact_pin', False), recorded
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_pending_follow_intent_moves_with_compaction_unconsumed(home, monkeypatch):
    """A committed profile change whose intent no build consumed yet, then a compaction: the next turn
    builds on the new physical target with THAT committed event (moved, not minted again)."""
    import json as _json
    from agent.model_routing_switch_intent import ORDER_KEY, PREFIX as INTENT
    from gateway.session_profile_selection import commit_selection
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        assert (await _turn(authority, owner, ref, built, 'opus'))[:2] == (OPUS, 'anthropic'), built
        _profile_pick(home, ASTRA, 'openai-codex')
        policy, config, model, ended = _selection_inputs(authority, ref)
        commit_selection(authority, ref, policy, config, model,
                         db.get_session(ref.session_id)['runtime_generation'], ended=ended)
        old = authority.physical_target(ref)
        event = _json.loads(db.get_meta(INTENT + old))['event']
        order = db.get_meta(ORDER_KEY)
        tip = _compress(db, authority, ref, 'tip')
        assert await _turn(authority, owner, ref, built, 'astra') == (ASTRA, 'openai-codex', tip), built
        assert db.get_meta(ORDER_KEY) == order  # no fresh event was minted
        assert db.get_meta(INTENT + old) is None and db.get_meta(INTENT + tip) is None
        fences = _routing_ledger(home, tip)[2]
        assert [f[2].split(':')[2:4] for f in fences] == [[event['store'], f"{event['seq']:020d}"]], fences
        assert _allocation(home, tip)['selected_model'] == ASTRA
    finally:
        await owner.close()
        store.close_all_db_handles()


# ---- managed path: the real admission + reservation ------------------------------------------------


def _hello(process):
    from hermes_cli.process_identity import worker_birth
    return {'type': 'hello', 'pid': process.pid, 'birth': worker_birth(psutil.Process(process.pid)),
            'ancestors': [os.getpid()]}


def _managed_turn(authority, db, ref, monkeypatch, rid):
    """One managed turn on a REAL admission and reservation; returns the worker's agent and scope."""
    import threading
    import hermes_state_runtime as rt
    from agent import managed_worker as worker
    from gateway.session_managed_worker import _bootstrap
    from gateway.session_worker_reservation import reserve_admission_worker
    import run_agent
    rt.admit_session_input(db, epoch=authority.epoch, principal_id='human', session_id=ref.session_id,
                           request_id=rid, payload={'text': rid})
    row = rt.claim_session_input(db, epoch=authority.epoch, session_id=ref.session_id)
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        scope = reserve_admission_worker(authority, admission_id=row['admission_id'], process=child,
                                         principal_id=row['principal_id'], hello=_hello(child))
    finally:
        child.kill()
        child.wait(timeout=5)
    frame = _bootstrap(authority, ref, row, _live_policy(authority, ref), scope)
    captured, sent = [], []

    class Agent(run_agent.AIAgent):
        def run_conversation(self, *args, **kwargs):
            captured.append(self)
            return {'final_response': 'ok', 'completed': True}

    class Controls:
        def __init__(self, *args):
            self.stopped, self.finish = threading.Event(), threading.Event()
            self.finish.set()
        def approval(self, data): pass
        def clarify(self, questions): pass

    def rpc_factory(home):
        def rpc(method, **params):
            if method == 'worker.adopt':
                return {'owner_epoch': authority.epoch}
            return rt.mutate_worker_execution(db, **{k: v for k, v in params.items() if k in PERSIST_FIELDS})
        return rpc

    with monkeypatch.context() as m:
        m.setattr(worker, 'bind_worker_policy', lambda frame: None)
        m.setattr(worker, 'discover_profile_mcp', lambda policy: None)
        m.setattr(worker, 'retire_agent', lambda agent: None)
        m.setattr(worker, 'WorkerControls', Controls)
        m.setattr('agent.runtime_session_store.WorkerRPC', rpc_factory)
        m.setattr('tools.process_registry.process_registry.recover_from_checkpoint', lambda: None)
        m.setattr('run_agent.AIAgent', Agent)
        worker.execute(frame, SimpleNamespace(send=lambda kind, **payload: sent.append(kind)))
    assert len(captured) == 1 and sent[-1] == 'finished', sent
    rt.settle_session_input(db, epoch=authority.epoch, admission_id=row['admission_id'],
                            generation=row['generation'], outcome='completed', result={'final_response': 'ok'})
    return captured[0], scope


@pytest.mark.asyncio
async def test_managed_follow_after_compression_uses_the_reserved_physical_target(home, monkeypatch):
    """The reservation's scope runs on the physical tip; the committed selection must name that id."""
    from tests.gateway.test_model_switch_worker_composed import _noop
    store, db, authority, owner, ref = _owner(home, monkeypatch, _noop)
    authority.runner._peek_session_state = lambda key: None
    authority.runner.session_authority = authority
    try:
        db.set_session_title(ref.session_id, 'Bot Chat')
        await _open(authority, ref)
        agent, scope = _managed_turn(authority, db, ref, monkeypatch, 'opus')
        assert (agent.model, agent.provider) == (OPUS, 'anthropic')
        assert _allocation(home, ref.session_id)['exact_pin'] is True
        tip = _compress(db, authority, ref, 'tip')
        _profile_pick(home, ASTRA, 'openai-codex')
        agent, scope = _managed_turn(authority, db, ref, monkeypatch, 'astra')
        assert scope['session_id'] == tip
        assert (agent.model, agent.provider, agent.session_id) == (ASTRA, 'openai-codex', tip)
        _profile_pick(home, OPUS, 'anthropic')
        agent, scope = _managed_turn(authority, db, ref, monkeypatch, 'back')
        assert (agent.model, agent.provider) == (OPUS, 'anthropic')
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('event', ['compaction', 'missing_memo'])
async def test_managed_unchanged_class_selection_never_rerolls(home, monkeypatch, event):
    from agent.model_routing_switch_intent import PREFIX as INTENT
    from tests.gateway.test_model_switch_worker_composed import _noop
    store, db, authority, owner, ref = _owner(home, monkeypatch, _noop)
    authority.runner._peek_session_state = lambda key: None
    authority.runner.session_authority = authority
    try:
        db.set_session_title(ref.session_id, 'Bot Chat')
        await _open(authority, ref)
        _managed_turn(authority, db, ref, monkeypatch, 'opus')
        _class_default(home)
        _managed_turn(authority, db, ref, monkeypatch, 'c1')
        before, _ = _managed_turn(authority, db, ref, monkeypatch, 'c2')
        ledger = _routing_ledger(home, authority.physical_target(ref))
        if event == 'compaction':
            _compress(db, authority, ref, 'tip')
        else:
            _forget_selection(db, ref)
        after, scope = _managed_turn(authority, db, ref, monkeypatch, 'c3')
        assert scope['session_id'] == authority.physical_target(ref)
        assert (after.model, after.provider) == (before.model, before.provider)
        assert _routing_ledger(home, scope['session_id']) == ledger
        assert db.get_meta(INTENT + scope['session_id']) is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_hold_ending_onto_a_class_default_never_rerolls_after(home, monkeypatch):
    """A held pick ends on an edit to a class default: the ending turn follows the class once, and the
    turns after it (no further edit) keep that allocation, its provider and the allocator untouched."""
    from agent.model_routing_switch_intent import PREFIX as INTENT
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        db.patch_session_model_config(ref.session_id, {
            'composer_override_profile': {'model': ASTRA, 'provider': 'openai-codex'}})
        _profile_pick(home, ASTRA, 'openai-codex')
        await _open(authority, ref)
        assert (await _turn(authority, owner, ref, built, 'held'))[:2] == (OPUS, 'anthropic'), built
        _class_default(home)
        ended = await _turn(authority, owner, ref, built, 'ended')
        assert ended[0] != 'raised', ended
        assert 'selection_profile' not in _receipt_model(db, ref)
        physical = authority.physical_target(ref)
        ledger = _routing_ledger(home, physical)
        assert not ledger[0].get('exact_pin', False), ledger
        for rid in ('c2', 'c3'):
            assert (await _turn(authority, owner, ref, built, rid))[:2] == ended[:2], built
            assert _routing_ledger(home, physical) == ledger
            assert db.get_meta(INTENT + physical) is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_managed_hold_ending_onto_a_class_default_never_rerolls_after(home, monkeypatch):
    from agent.model_routing_switch_intent import PREFIX as INTENT
    from tests.gateway.test_model_switch_worker_composed import _noop
    store, db, authority, owner, ref = _owner(home, monkeypatch, _noop)
    authority.runner._peek_session_state = lambda key: None
    authority.runner.session_authority = authority
    try:
        db.set_session_title(ref.session_id, 'Bot Chat')
        db.patch_session_model_config(ref.session_id, {
            'composer_override_profile': {'model': ASTRA, 'provider': 'openai-codex'}})
        _profile_pick(home, ASTRA, 'openai-codex')
        await _open(authority, ref)
        held, _ = _managed_turn(authority, db, ref, monkeypatch, 'held')
        assert (held.model, held.provider) == (OPUS, 'anthropic')
        _class_default(home)
        ended, scope = _managed_turn(authority, db, ref, monkeypatch, 'ended')
        assert 'selection_profile' not in _receipt_model(db, ref)
        ledger = _routing_ledger(home, scope['session_id'])
        assert not ledger[0].get('exact_pin', False), ledger
        for rid in ('c2', 'c3'):
            after, scope = _managed_turn(authority, db, ref, monkeypatch, rid)
            assert (after.model, after.provider) == (ended.model, ended.provider)
            assert _routing_ledger(home, scope['session_id']) == ledger
            assert db.get_meta(INTENT + scope['session_id']) is None
    finally:
        await owner.close()
        store.close_all_db_handles()


# ---- owner stale controls -------------------------------------------------------------------------


def _selection_inputs(authority, ref):
    policy = _live_policy(authority, ref)
    config, model, ended = policy.effective_selection(authority, secrets=False)
    return policy, config, model, ended


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', ['epoch', 'generation', 'policy'])
async def test_commit_refuses_stale_owner_generation_or_policy(home, monkeypatch, stale):
    import hermes_state_runtime as rt
    from agent.model_routing_switch_intent import PREFIX
    from dataclasses import replace
    from gateway.session_profile_selection import commit_selection
    from hermes_state_runtime import RuntimeStoreError
    store, db, authority, owner, ref, _built = _bot_chat_owner(home, monkeypatch)
    try:
        await _open(authority, ref)
        _profile_pick(home, ASTRA, 'openai-codex')
        policy, config, model, ended = _selection_inputs(authority, ref)
        generation = db.get_session(ref.session_id)['runtime_generation']
        if stale == 'epoch':
            rt.begin_runtime_epoch(db, instance_id='successor')
        elif stale == 'generation':
            generation -= 1
        else:
            policy = replace(policy, cwd=policy.cwd + '-other')
        with pytest.raises(RuntimeStoreError):
            commit_selection(authority, ref, policy, config, model, generation, ended=ended)
        assert db.get_meta(PREFIX + authority.physical_target(ref)) is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_prepared_mutation_racing_a_hold_release_is_refused(home, monkeypatch):
    """A mutation prepared against the receipt that still held the pick fails its snapshot check once
    the owner commits the hold's end, instead of writing the hold back; a retry then applies."""
    import hermes_cli.model_switch as ms
    store, db, authority, owner, ref, built = _bot_chat_owner(home, monkeypatch)
    try:
        db.patch_session_model_config(ref.session_id, {
            'composer_override_profile': {'model': ASTRA, 'provider': 'openai-codex'}})
        _profile_pick(home, ASTRA, 'openai-codex')
        await _open(authority, ref)
        assert (await _turn(authority, owner, ref, built, 'held'))[:2] == (OPUS, 'anthropic'), built
        _profile_pick(home, OPUS, 'anthropic')  # the hold has now ended, not yet committed
        real = ms.switch_model
        from gateway.session_profile_selection import commit_selection

        def switch_then_release(**kwargs):
            result = real(**kwargs)
            policy, config, model, ended = _selection_inputs(authority, ref)
            assert ended
            commit_selection(authority, ref, policy, config, model,
                             db.get_session(ref.session_id)['runtime_generation'], ended=True)
            return result
        monkeypatch.setattr(ms, 'switch_model', switch_then_release)
        reply = await _mutate(owner, db, ref, 'racing', {'model': ASTRA, 'provider': 'openai-codex'})
        assert 'error' in reply and 'revision_conflict' in json.dumps(reply['error']), reply
        assert 'selection_profile' not in _receipt_model(db, ref)
        monkeypatch.setattr(ms, 'switch_model', real)
        reply = await _mutate(owner, db, ref, 'retry', {'model': ASTRA, 'provider': 'openai-codex'})
        assert reply['result']['model'] == ASTRA, reply
        assert (await _turn(authority, owner, ref, built, 'picked'))[:2] == (ASTRA, 'openai-codex'), built
    finally:
        await owner.close()
        store.close_all_db_handles()
