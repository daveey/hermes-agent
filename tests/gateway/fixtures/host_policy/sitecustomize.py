"""Disposable host worker policy for every Python process a Kanban probe spawns.

Loaded as ``sitecustomize`` through the ``PYTHONPATH`` entry ``local_recovery_probe.isolate_host_policy``
adds to the probe's environment. ``worker_compute_capacity.policy_root()`` is the OS account's
``~/.hermes`` by design (independent of HOME and HERMES_HOME), so without this a probe's gateway owners
and workers on a worker host read its real ``kanban.worker_resources`` policy and inventory: the owner
then placed the card's worker in a CPU slice the disposable run never has and the turn never started.
"""

import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

_ROOT = os.environ.get('HERMES_PROBE_HOST_POLICY_ROOT')
_MODULE = 'hermes_cli.worker_compute_capacity'


def _patch(module):
    root = Path(_ROOT or '')
    module.policy_root = lambda: root
    # The probe's test asserts its children read this root, not just the probe process itself.
    root.mkdir(parents=True, exist_ok=True)
    (root / f'child-{os.getpid()}').touch()


class _Loader(importlib.abc.Loader):
    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        _patch(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != _MODULE:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _Loader(spec.loader)
        return spec


if _ROOT:
    if _MODULE in sys.modules:
        _patch(sys.modules[_MODULE])
    else:
        sys.meta_path.insert(0, _Finder())
