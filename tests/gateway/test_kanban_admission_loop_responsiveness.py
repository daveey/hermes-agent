"""Cold board policy reads must not prevent the owner answering its submitters."""
import asyncio
import threading

from tests.gateway import test_kanban_finite_admission as helpers

kanban_owner = helpers.kanban_owner


def test_policy_construction_is_off_owner_loop_and_replay_has_one_admission(kanban_owner, monkeypatch):
    from gateway import session_kanban as sk

    async def scenario():
        owner = kanban_owner(helpers._CONFIG)
        loop_thread = threading.get_ident()
        original = sk.build_kanban_policy
        calls = []

        async def viewer_resume(*_args):
            raise TimeoutError('viewer history/inventory must not gate a dispatcher receipt')

        monkeypatch.setattr(owner.connection, 'resume', viewer_resume)

        def build(*args):
            calls.append(threading.get_ident())
            assert threading.get_ident() != loop_thread, 'board policy construction blocks admission RPCs'
            return original(*args)

        monkeypatch.setattr(sk, 'build_kanban_policy', build)
        try:
            first = await sk.run_task(owner.connection, owner.params)
            # The first answer can be lost in transport; retry must return its original receipt.
            second = await sk.run_task(owner.connection, owner.params)
            assert second['session_id'] == first['session_id']
            assert second['receipt']['admission_id'] == first['receipt']['admission_id']
            assert len(calls) == 1
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_slow_policy_does_not_time_out_an_owner_control_request(kanban_owner, monkeypatch):
    from gateway import session_kanban as sk

    async def scenario():
        owner = kanban_owner(helpers._CONFIG)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        original = sk.build_kanban_policy

        def build(*args):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise TimeoutError('owner loop could not answer while policy was reading')
            return original(*args)

        monkeypatch.setattr(sk, 'build_kanban_policy', build)
        admission = asyncio.create_task(sk.run_task(owner.connection, owner.params))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            answered = loop.create_future()
            loop.call_soon(answered.set_result, True)
            assert await asyncio.wait_for(answered, .5)
            assert not admission.done()
        finally:
            release.set()
            try:
                await admission
            finally:
                await owner.close()

    asyncio.run(scenario())
