"""Owned source/transport teardown retains all failure custody. [daveey]"""
from types import SimpleNamespace

import pytest

from hermes_cli.fact_view_owner import ViewOwner


def test_failed_reader_close_still_stops_transport_and_capture():
    calls = []
    def fail():
        calls.append('reader')
        raise TimeoutError('reader busy')
    owner = object.__new__(ViewOwner)
    owner.handle = SimpleNamespace(close=fail)
    owner.transport = SimpleNamespace(close=lambda: calls.append('transport'))
    owner.owner = SimpleNamespace(close=lambda: calls.append('source'))
    with pytest.raises(ExceptionGroup, match='shutdown incomplete') as error:
        owner.close()
    assert calls == ['reader', 'transport', 'source']
    assert isinstance(error.value.exceptions[0], TimeoutError)
    owner.handle = SimpleNamespace(close=lambda: calls.append('reader-stopped'))
    owner.close()
    assert calls[-3:] == ['reader-stopped', 'transport', 'source']
