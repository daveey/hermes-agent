"""Composed producer event dispatch retains legacy and partitioned provider signatures."""
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_process as process_mod
from hermes_cli.serve_producer_runtime import ProjectionRelay


@pytest.mark.parametrize('partition,with_poll', [(None, True), ({'home': '/a'}, True), (None, False)])
def test_event_dispatch_signature_and_absent_provider(tmp_path, monkeypatch, partition, with_poll):
    calls = []
    if with_poll:
        if partition is None:
            def poll(after, limit):
                calls.append((after, limit))
                return {'seq': 5, 'events': [], 'reset': True}
        else:
            def poll(after, limit, *, partition):
                calls.append((after, limit, partition))
                return {'seq': 5, 'events': [], 'reset': True}
        target = SimpleNamespace(events_since=poll)
    else:
        target = SimpleNamespace()
    monkeypatch.setattr(process_mod, 'load_provider', lambda config: SimpleNamespace(open_provider=lambda config: target))
    monkeypatch.setattr(process_mod, 'authorized', lambda request, config: True)
    frames = []
    monkeypatch.setattr('hermes_cli.serve_producer_protocol.encode_request', lambda key, response: frames.append(response) or [b'frame'])
    requests = iter([{'verb': 'EVENTS', 'after': -1, 'partition': partition}, {'verb': 'STOP'}])
    channel = SimpleNamespace(send=lambda *args: None, receive=lambda: next(requests),
                              sock=SimpleNamespace(sendall=lambda frame: None))
    process_mod.producer(channel, {'provider': {}, 'runtime': str(tmp_path), 'generation': 'g'})
    assert frames == [{'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': 5 if with_poll else 0,
                       'events': [], 'reset': with_poll}]
    expected = [(-1, 64)] if partition is None else [(-1, 64, partition)]
    assert calls == (expected if with_poll else [])


@pytest.mark.parametrize('provider', [None, [], 'provider'])
def test_nonmapping_provider_is_not_partitioned(tmp_path, provider):
    relay = ProjectionRelay(provider, home=tmp_path, runtime=tmp_path / 'r', generation='g')
    assert relay.partitioned is False
