"""Demand custody across real Unix-socket owner lifetimes."""
import socket
import queue
import threading
from pathlib import Path

import pytest

from hermes_cli.fact_contract import Fact, Ready, Scope
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint
from hermes_cli.fact_store import FactStore, view_key
from tests.waiting import wait_until


@pytest.mark.platforms('posix')
def test_accepted_demand_survives_owner_restart_not_same_owner_reconnect(tmp_path, monkeypatch):
    scope = Scope('install', 'profile')
    token = 't' * 32
    monkeypatch.chdir(tmp_path)
    directory = Path('owner')
    calls = []

    def start_owner():
        store = FactStore()
        def demand(got_scope, view, selector):
            calls.append((store.snapshot(scope).epoch, view))
            store.publish([Fact(view_key(got_scope, view, selector), {'encoded': b'{"ok":true}'}, health='fresh')])
        return FactHost(directory, {token: scope}, store=store, demand=demand).start()

    owner = start_owner()
    handle = acquire(scope, endpoint=Endpoint(str(directory / 'facts.sock'), token))
    try:
        handle.demand('title', {})
        assert wait_until(lambda: isinstance(handle.v1.snapshot(scope).view('title', {}), Ready), timeout=5)
        first_epoch = owner.store.snapshot(scope).epoch
        old_socket = handle._socket
        old_socket.shutdown(socket.SHUT_RDWR)
        assert wait_until(lambda: handle._socket is not None and handle._socket is not old_socket
                          and handle._online, timeout=5)
        # A barrier demand drains any erroneously replayed commands first.
        handle.demand('barrier', {})
        assert wait_until(lambda: isinstance(handle.v1.snapshot(scope).view('barrier', {}), Ready), timeout=5)
        assert calls.count((first_epoch, 'title')) == 1
        owner.close()
        owner = start_owner()
        second_epoch = owner.store.snapshot(scope).epoch
        assert second_epoch != first_epoch
        assert wait_until(lambda: handle.v1.snapshot(scope).epoch == second_epoch, timeout=5)
        assert wait_until(lambda: isinstance(handle.v1.snapshot(scope).view('title', {}), Ready), timeout=5)
        assert calls.count((second_epoch, 'title')) == 1
    finally:
        handle.close()
        owner.close()


@pytest.mark.platforms('posix')
def test_old_ack_after_new_owner_reset_replays(tmp_path, monkeypatch):
    from hermes_cli import fact_demand
    scope = Scope('install', 'profile')
    token = 't' * 32
    monkeypatch.chdir(tmp_path)
    ack_received, release_ack = threading.Event(), threading.Event()
    original_receive = fact_demand.receive

    def delayed_receive(sock):
        result = original_receive(sock)
        if isinstance(result, dict) and result.get('accepted') and not ack_received.is_set():
            ack_received.set()
            assert release_ack.wait(5)
        return result

    monkeypatch.setattr(fact_demand, 'receive', delayed_receive)
    owner = FactHost('owner', {token: scope}, demand=lambda *args: None).start()
    handle = acquire(scope, endpoint=Endpoint('owner/facts.sock', token))
    replayed = threading.Event()
    try:
        handle.demand('title', {})
        assert ack_received.wait(5)
        owner.close()
        owner = FactHost('owner', {token: scope}, demand=lambda *args: replayed.set()).start()
        epoch = owner.store.snapshot(scope).epoch
        assert wait_until(lambda: handle._demands._epoch == epoch, timeout=5)
        release_ack.set()
        assert replayed.wait(5)
    finally:
        release_ack.set()
        handle.close()
        owner.close()


def test_active_demand_bound_expiry_rejection_and_shutdown(monkeypatch):
    from hermes_cli import fact_demand
    now = [0.0]
    monkeypatch.setattr(fact_demand, 'monotonic', lambda: now[0])
    calls = []
    def deliver(self, command):
        calls.append(command['view'])
        return 'owner', command['view'] != 'rejected'
    monkeypatch.setattr(fact_demand.DemandChannel, 'deliver', deliver)
    channel = fact_demand.DemandChannel(Scope('install', 'profile'), None)
    try:
        channel.owner_epoch('owner')
        for i in range(64):
            channel.put(str(i), {})
        assert wait_until(lambda: len(calls) == 64, timeout=5)
        # ACK does not release the capacity reserved for replay custody.
        with pytest.raises(queue.Full):
            channel.put('overflow', {})
        channel.put('0', {})  # Duplicate refresh is allowed even at capacity.
        channel.owner_epoch('next')
        assert wait_until(lambda: len(calls) == 128, timeout=5)
        now[0] = 30.0
        channel.owner_epoch('expired')
        channel.put('rejected', {})
        assert wait_until(lambda: not channel._active, timeout=5)
        channel.put('barrier', {})
        assert wait_until(lambda: calls[-1:] == ['barrier'], timeout=5)
        assert calls.count('0') == 2  # Expired demand was not replayed.
        assert calls.count('rejected') == 1
    finally:
        channel.close()
    assert not channel.thread.is_alive()
    assert not channel._active
    with pytest.raises(RuntimeError, match='closed'):
        channel.put('late', {})
