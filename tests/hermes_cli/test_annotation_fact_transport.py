"""Real owner/replica annotation proof with a test-only lifecycle facade.

Does not certify production source adapters, waiting-source registration,
global activation, or actual legacy-holder rollback. Those remain integration gates.
"""
import multiprocessing

import pytest

from tests.hermes_cli.annotation_fact_processes import owner, replica
from tests.waiting import wait_until


def start(ctx, target, *args):
    parent, child = ctx.Pipe()
    process = ctx.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    assert parent.poll(15), 'child startup timed out'
    return process, parent, parent.recv()


def rpc(pipe, command):
    pipe.send(command)
    assert pipe.poll(15), 'child command timed out'
    return pipe.recv()


def stop(process, pipe, command):
    if process.is_alive():
        pipe.send(command)
        process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)
    pipe.close()


def matches(pipe, expected):
    result = rpc(pipe, 'read')
    return result.get('rows') == [expected] and result['io'] == []


@pytest.mark.platforms('linux')
def test_annotation_single_owner_replicas_mutation_and_failover(tmp_path_factory):
    ctx = multiprocessing.get_context('spawn')
    directory = tmp_path_factory.mktemp('af')
    producer, op, ready = start(ctx, owner, directory)
    assert ready == {'ready': True}
    clients = []
    try:
        for index in (0, 0, 1):
            process, pipe, ready = start(ctx, replica, directory, index)
            clients.append((process, pipe))
            assert ready == {'ready': True}
        serve, gateway, other = [pipe for _, pipe in clients]
        contender, cp, rejected = start(ctx, owner, directory)
        contender.join(5)
        cp.close()
        assert rejected == {'error': 'BlockingIOError'}
        assert contender.exitcode == 0
        alpha = rpc(op, ('capture', 0, ['one']))['expected']
        beta = rpc(op, ('capture', 1, ['private']))['expected']
        assert wait_until(lambda: matches(serve, alpha) and matches(gateway, alpha), timeout=15)
        assert wait_until(lambda: matches(other, beta), timeout=15)
        assert rpc(serve, 'privacy') == rpc(other, 'privacy') == {'denied': True}
        for _ in range(20):
            assert matches(serve, alpha) and matches(gateway, alpha)
        assert rpc(op, ('stats',))['reads'] == 2
        updated = rpc(op, ('capture', 0, ['two', 'three']))['expected']
        assert wait_until(lambda: matches(serve, updated) and matches(gateway, updated), timeout=15)
        assert matches(other, beta)
        assert rpc(op, ('stats',))['reads'] == 3
        assert updated['kanban_waiting_on'] == ['three', 'two']
        epoch = rpc(serve, 'read')['epoch']
        producer.terminate()  # fixture-owned owner, never a live service
        producer.join(5)
        op.close()
        assert wait_until(lambda: rpc(serve, 'read').get('unavailable'), timeout=10)
        assert wait_until(lambda: rpc(gateway, 'read').get('unavailable'), timeout=10)
        producer, op, ready = start(ctx, owner, directory)
        assert ready == {'ready': True}
        successor = rpc(op, ('capture', 0, ['successor']))['expected']
        assert wait_until(lambda: matches(serve, successor) and matches(gateway, successor), timeout=15)
        assert rpc(serve, 'read')['epoch'] != epoch
        assert rpc(op, ('stats',))['reads'] == 1
        for _, pipe in clients:
            assert rpc(pipe, 'rollback') == {'lifecycle': ['stop', 'start'], 'subscription': False}
    finally:
        for process, pipe in clients:
            stop(process, pipe, 'stop')
        stop(producer, op, ('stop',))
    assert producer.exitcode == 0
    assert all(process.exitcode == 0 for process, _ in clients)
