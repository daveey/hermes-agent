"""Factories are part of producer bootstrap, not a way around the loaded-digest check."""
import pytest

from hermes_cli import serve_producer_broker as shared
from tests.hermes_cli.test_serve_producer_shared_broker import start, write_provider, produce


@pytest.mark.parametrize('declared', [False, True])
def test_factory_imports_are_verified_before_shared_ready(tmp_path, declared):
    helper = tmp_path / 'factory_helper.py'
    helper.write_text('VALUE = 7\n')
    source = ('import os,sys\n'
              'def open_provider(config):\n'
              '    sys.path.insert(0, os.path.dirname(__file__))\n'
              '    import factory_helper\n'
              '    return lambda payload: {"value": factory_helper.VALUE}\n')
    spec = write_provider(tmp_path, source, extra=(helper,) if declared else ())
    relay = None
    try:
        if not declared:
            with pytest.raises(shared.OwnerUnavailable):
                relay = start(spec, tmp_path, timeout=3)
            assert shared.read_owner(tmp_path / 'runtime') is None
        else:
            relay = start(spec, tmp_path)
            assert produce(relay, x=1) == {'value': 7}
    finally:
        if relay is not None:
            assert relay.retire(timeout=10)
