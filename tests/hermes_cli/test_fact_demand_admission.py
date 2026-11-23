"""Demand admission remains available at the stream connection limit."""
import threading

import pytest

from hermes_cli.fact_contract import Scope
from hermes_cli.fact_demand import DemandChannel
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_ipc import connect, receive, send
from hermes_cli.fact_replica import Endpoint
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
def test_eight_healthy_replicas_do_not_starve_demands(tmp_path_factory):
    scope = Scope('fixture', 'alpha')
    token = 'demand-fixture-capability-token-000000'
    delivered = threading.Event()
    seen = []

    def demand(actual_scope, view, selector):
        seen.append((actual_scope, view, selector))
        delivered.set()

    host = FactHost(tmp_path_factory.mktemp('admit'), {token: scope}, demand=demand).start()
    endpoint = Endpoint(str(host.directory / 'facts.sock'), token)
    replicas = []
    channel = None
    try:
        for _ in range(8):
            replica = acquire(scope, endpoint=endpoint)
            replicas.append(replica)
            assert wait_until(lambda: replica._online, timeout=5)
        channel = DemandChannel(scope, endpoint)
        channel.put('annotations', {'session': 'one'})
        assert delivered.wait(3), 'eight healthy streams must leave demand capacity'
        assert seen == [(scope, 'annotations', {'session': 'one'})]
        assert all(replica._online for replica in replicas)
    finally:
        if channel is not None:
            channel.close()
        host.close()
        for replica in replicas:
            replica.close()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('channel,token_ok,op', [
    ('demand', True, 'next'), ('demand', True, 'page'),
    ('demand', True, 'release'), ('unknown', True, None),
    ('demand', False, None),
])
def test_control_channel_cannot_bypass_scope_or_stream_limits(tmp_path_factory, channel, token_ok, op):
    scope = Scope('fixture', 'alpha')
    token = 'control-fixture-capability-token-0000'
    host = FactHost(tmp_path_factory.mktemp('ctrl'), {token: scope}).start()
    try:
        with connect(str(host.directory / 'facts.sock')) as sock:
            sock.settimeout(2)
            send(sock, {'version': 1, 'token': token if token_ok else 'wrong', 'channel': channel})
            if op is not None:
                assert receive(sock)['scope'] == scope
                send(sock, {'op': op})
            with pytest.raises((EOFError, ConnectionResetError)):
                receive(sock)
    finally:
        host.close()


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('channel,limit', [('stream', 8), ('demand', 16)])
def test_admission_is_bounded_and_disconnect_releases_capacity(tmp_path_factory, channel, limit):
    scope = Scope('fixture', 'alpha')
    token = 'bounded-fixture-capability-token-0000'
    host = FactHost(tmp_path_factory.mktemp('bound'), {token: scope}).start()
    path = str(host.directory / 'facts.sock')
    sockets = []

    def open_channel():
        sock = connect(path)
        sockets.append(sock)
        sock.settimeout(2)
        send(sock, {'version': 1, 'token': token, 'channel': channel})
        return sock

    try:
        for _ in range(limit):
            assert receive(open_channel())['scope'] == scope
        with pytest.raises((EOFError, ConnectionResetError, BrokenPipeError)):
            receive(open_channel())
        sockets[0].close()
        # Inspect capacity only to synchronize the real disconnect handler;
        # the next real handshake below proves the admission is reusable.
        semaphore = host._server.stream_slots if channel == 'stream' else host._server.slots
        assert wait_until(lambda: semaphore._value > 0, timeout=2)
        assert receive(open_channel())['scope'] == scope
    finally:
        for sock in sockets:
            sock.close()
        host.close()
