"""Review records exercised through a real supported boot and directory loader."""
import hashlib
import json
import shutil

import pytest

from tests.hermes_cli import test_code_attestation_route as routes

core = routes.core
python = routes.python
pytestmark = pytest.mark.live_system_guard_bypass

PLUGIN = 'from dataclasses import dataclass\n@dataclass\nclass Value:\n    number: int = 7\n'
PROBE = '''import json, os, time
from pathlib import Path
from types import SimpleNamespace
from hermes_cli import code_attestation as ca
from hermes_cli.plugins_loader import PluginLoaderMixin
from gateway.capsule_contract import available_capabilities, _verified_sha
from hermes_cli.kanban_capsule_owner import CONTRACT_CAPABILITY
root = Path(os.environ['PLUGIN_ROOT'])
mode = os.environ['PLUGIN_MODE']
manifest = SimpleNamespace(path=root, name='review-fixture', key='review-fixture')
loader = PluginLoaderMixin()
if mode == 'changed':
    (root / '__init__.py').write_text('value = 999\\n')
if mode == 'missing':
    (root / 'unused.py').unlink()
module = loader._load_directory_module(manifest, module_name='hermes_plugins.review_fixture')
if mode == 'unrooted':
    import sys
    sys.path.insert(0, str(root.parent / 'outside'))
    import outside_dependency
if mode == 'omitted':
    import importlib.util
    spec = importlib.util.spec_from_file_location('hermes_plugins.extra', root / 'extra.py')
    extra = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extra)
if mode == 'conflict':
    from hermes_cli.plugin_review_records import register_directory
    register_directory('review-fixture', root.parent / 'other-plugin')
if mode == 'after':
    (root / '__init__.py').write_text('value = 999\\n')
if mode == 'reload':
    module = loader._load_directory_module(manifest, module_name='hermes_plugins.review_fixture')
sha = ca._ledger().boot_sha
started = time.monotonic()
caps = available_capabilities([CONTRACT_CAPABILITY], advertised_sha=sha)
elapsed = time.monotonic() - started
from hermes_cli import git_fingerprint as gf
import subprocess
from gateway.capsule_contract import CORE_ROOT
json.dump({'boot': gf._capsule_boot, 'core_root': str(CORE_ROOT),
           'disk': gf._capsule_clean_sha(CORE_ROOT),
           'status': subprocess.check_output(['git', '-C', str(CORE_ROOT), 'status', '--porcelain'], text=True),
           'caps': caps, 'verified': _verified_sha(), 'elapsed': elapsed,
           'unknowns': ca.integrity_observation()['unknowns'], 'sha': sha},
          open(os.environ['ATTEST_PROBE_OUT'], 'w'))
'''


@pytest.mark.parametrize('mode', ['reviewed', 'reload', 'changed', 'missing', 'after', 'unreviewed', 'unrooted',
                                 'omitted', 'conflict'])
def test_supported_plugin_boot(tmp_path, core, python, mode):
    tree = tmp_path / 'core'
    shutil.copytree(core, tree)
    shutil.copy2(routes.ROOT / '.gitignore', tree / '.gitignore')
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / '__init__.py').write_text(PLUGIN)
    (plugin / 'unused.py').write_text('unused = True\n')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'outside_dependency.py').write_text('value = 1\n')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in plugin.iterdir()}
    shutil.copytree(plugin, tmp_path / 'other-plugin')
    (plugin / 'extra.py').write_text('extra = True\n')
    records = {} if mode == 'unreviewed' else {'review-fixture': files}
    (tree / 'hermes_cli/data/plugin-review-records.json').write_text(json.dumps({'version': 1, 'plugins': records}))
    (tree / 'attest_probe.py').write_text(PROBE)
    routes._git(tree, 'add', '-A')
    routes._git(tree, 'commit', '-qm', 'review fixture')
    result = routes.run_stub(tmp_path, python, routes.current_script(tree), extra_env={
        'PLUGIN_ROOT': str(plugin), 'PLUGIN_MODE': mode})
    if mode in ('reviewed', 'reload'):
        assert result['unknowns'] == []
        assert result['status'] == '', result['status']
        assert result['verified'] == result['sha'], result
        assert result['caps'] == ['kanban-capsule-contract-v2']
    else:
        assert result['unknowns']
        assert result['verified'] is None
        assert result['caps'] == []
    assert result['elapsed'] < 0.5
