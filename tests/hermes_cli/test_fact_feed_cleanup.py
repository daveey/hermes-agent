"""Failed adapter teardown remains reachable through owner retry."""
import pytest

from hermes_cli.fact_feed import ChangeFeed
from tests.hermes_cli.test_fact_feed import Adapter


def test_close_retries_failed_watch_and_closes_other_sources():
    first, second = Adapter(), Adapter()
    attempts = []
    def close():
        attempts.append(True)
        if len(attempts) == 1:
            raise TimeoutError('worker still active')
        first.closed = True
    first.close = close
    feed = ChangeFeed(lambda *args: None, lambda *args: None)
    feed.register(first)
    feed.register(second)
    with pytest.raises(TimeoutError):
        feed.close()
    assert second.closed
    assert any(source.adapter is first for source in feed._sources)
    feed.close()
    assert len(attempts) == 2 and first.closed
    assert not feed._sources
