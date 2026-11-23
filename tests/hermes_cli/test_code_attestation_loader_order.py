"""Import the real plugin loader only after the supported bootstrap boundary."""
import json
import os
import subprocess
import sys

from tests.hermes_cli import test_code_attestation_route as helpers

core = helpers.core
python = helpers.python
pytestmark = helpers.pytestmark


def test_supported_bootstrap_observes_loader_imports(tmp_path, core, python):
    probe = core / 'attest_probe.py'
    probe.write_text('from hermes_cli.plugins_loader import PluginLoaderMixin\n' + probe.read_text())
    helpers._git(core, 'add', '.')
    helpers._git(core, 'commit', '-qm', 'observe real loader after bootstrap')
    result = helpers.run_stub(tmp_path, python, helpers.current_script(core))
    assert result['report']['route'] == 'managed-stub'
    assert result['unknowns'] == [], result['unknowns']
    filenames = {unit['filename'] for unit in result['units']}
    assert str(core / 'hermes_cli/plugins_loader.py') in filenames


def test_late_direct_ledger_still_latches_preexisting_loader(tmp_path):
    script = tmp_path / 'late.py'
    script.write_text('''
import json, subprocess, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins_loader import PluginLoaderMixin
from hermes_cli.code_attestation import Attestation
root = sys.argv[1]
sha = subprocess.check_output(['git', '-C', root, 'rev-parse', 'HEAD'], text=True).strip()
a = Attestation(root, sha)
print(json.dumps(list(a.unknowns())))
''')
    result = subprocess.run([sys.executable, str(script), str(helpers.ROOT)], cwd=tmp_path,
        capture_output=True, text=True, timeout=60, check=False,
        env={'PATH': os.defpath, 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path),
             'HERMES_HOME': str(tmp_path / 'home')})
    assert result.returncode == 0, result.stdout + result.stderr
    reasons = json.loads(result.stdout)
    assert 'prehook-module:hermes_cli.plugins_loader' in reasons
    assert 'bootstrap-provenance-pending' in reasons
