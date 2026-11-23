"""Collection evidence only; explicitly loaded by the entry142 canonical runner."""
import json
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption('--entry142-prefix', default=None)


def pytest_collection_finish(session):
    prefix = session.config.getoption('--entry142-prefix')
    if prefix:
        nodes = [item.nodeid for item in session.items]
        name = Path(session.config.args[0]).name
        path = Path(prefix + '-' + name + '.nodes.json')
        with path.open('x') as stream:
            json.dump(nodes, stream, indent=2)
