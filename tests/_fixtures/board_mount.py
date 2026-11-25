"""Custody for a bare-router test that imports the full dashboard. [daveey]"""
from contextlib import contextmanager
import sys


def _registry():
    server = sys.modules.get('hermes_cli.web_server')
    app = getattr(server, 'app', None)
    return getattr(getattr(app, 'state', None), 'fact_plugin_lifecycles', None)


@contextmanager
def imported_dashboard_lifecycles():
    """Drain only mounts started by this block, before its home/patches change.

    A web_server import mounts bundled plugins even without entering its ASGI
    lifespan. Closing a TestClient for a different bare app cannot drain them.
    Never clear the provider global: real close must restore the prior binding.
    """
    previous = _registry()
    owned_before = set(previous._handles) if previous is not None else set()
    try:
        yield
    finally:
        registry = _registry()
        if registry is not None:
            for key in tuple(registry._handles):
                if registry is not previous or key not in owned_before:
                    registry.stop(key)
