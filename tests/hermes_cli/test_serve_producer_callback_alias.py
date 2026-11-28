"""Canonical source aliases preserve executable callback identity, including nested code."""
import importlib.util
from types import SimpleNamespace

from hermes_cli.serve_producer_callbacks import capture


def test_symlink_compilation_alias_and_nested_code_share_registry_identity(tmp_path):
    root = tmp_path / 'actual'
    root.mkdir()
    path = root / 'callbacks.py'
    path.write_text('def done(card):\n    def inner(): return "same"\n    return inner()\n')
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    snapshots = []
    for location in (path, alias / path.name):
        spec = importlib.util.spec_from_file_location('_alias_callbacks', location)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        activity = SimpleNamespace(_done_checks={'done': module.done}, _waiting_sources={})
        host = SimpleNamespace(_declared={}, _event_sources={}, _done_event_sources={})
        snapshots.append(capture(activity=activity, host=host))
    assert snapshots[0] == snapshots[1]
