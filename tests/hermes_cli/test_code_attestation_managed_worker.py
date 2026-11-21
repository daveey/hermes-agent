"""Fixed managed-worker launcher bootstraps before importing the agent package."""
import json

import pytest

from tests.hermes_cli import test_code_attestation_route as helpers

core = helpers.core
python = helpers.python
pytestmark = helpers.pytestmark


@pytest.fixture(scope='module')
def worker_core(core):
    worker = core / 'agent' / 'managed_worker.py'
    text = worker.read_text()
    marker = 'def main():\n'
    assert text.count(marker) == 1
    worker.write_text(text.replace(marker, marker + '''    # Fixture intercepts final dispatch only; production identity gates are real.
    from gateway.capsule_contract import CORE_ROOT, install_worker_contract, check_worker_identity
    from hermes_cli.git_fingerprint import process_boot_fingerprint
    import os
    try:
        install_worker_contract(['--observation-fd', '9', '--capsule-contract-v2',
                                 process_boot_fingerprint(CORE_ROOT).rsplit(':', 1)[-1]])
        if os.environ.get('CAPSULE_CASE') == 'dirty-revert':
            source = CORE_ROOT / 'capsule_import_fixture.py'
            before = source.read_bytes()
            try:
                source.write_text('VALUE = 2\\n')
                import capsule_import_fixture
                assert capsule_import_fixture.VALUE == 2
            finally:
                source.write_bytes(before)
        check_worker_identity()
        os.environ['CAPSULE_GATE_RESULT'] = 'accepted'
    except Exception as exc:
        from hermes_cli.code_attestation import integrity_observation
        from hermes_cli.git_fingerprint import capsule_code_sha
        os.environ['CAPSULE_GATE_RESULT'] = repr((str(exc), process_boot_fingerprint(CORE_ROOT),
            capsule_code_sha(CORE_ROOT), integrity_observation()))
    import attest_probe
    return 0
''', 1))
    probe = core / 'attest_probe.py'
    probe.write_text(probe.read_text().replace('"argv": sys.argv,',
        '"argv": sys.argv, "gate": os.environ.get("CAPSULE_GATE_RESULT"),'))
    # Match the production tree's ignored bytecode, not a permanently dirty boot.
    (core / '.gitignore').write_text('__pycache__/\n*.pyc\n')
    (core / 'capsule_import_fixture.py').write_text('VALUE = 1\n')
    helpers._git(core, 'add', '.')
    helpers._git(core, 'commit', '-qm', 'worker-final-dispatch-probe')
    return core


def test_fixed_worker_launcher_has_supported_provenance(tmp_path, worker_core, python):
    from hermes_cli import _launch_template
    core = worker_core
    worker = core / 'agent' / 'managed_worker.py'
    probe = helpers.run_stub(tmp_path, python,
                             _launch_template.render(str(core), 'agent.managed_worker', 'main'), args=())
    assert probe['report']['route'] == 'managed-worker-stub'
    assert probe['gate'] == 'accepted', probe['gate']
    assert probe['unknowns'] == [], json.dumps(probe['unknowns'])
    units = {unit['filename'] for unit in probe['units']}
    assert str(worker) in units
    assert str(core / 'agent' / '__init__.py') in units


def test_dirty_import_revert_still_refuses_execution(tmp_path, worker_core, python):
    from hermes_cli import _launch_template
    probe = helpers.run_stub(tmp_path, python,
        _launch_template.render(str(worker_core), 'agent.managed_worker', 'main'),
        args=(), extra_env={'CAPSULE_CASE': 'dirty-revert'})
    assert 'capsule_worker_code_unverified' in probe['gate'], probe['gate']
    assert 'None' not in probe['gate'].split("'core_root'")[0], probe['gate']
    assert any('capsule_import_fixture.py' in unknown for unknown in probe['unknowns']), probe['unknowns']
    assert helpers._git(worker_core, 'status', '--porcelain').strip() == ''


@pytest.mark.parametrize('variant', ['changed-tail', 'arbitrary-module', 'relaunch'])
def test_only_exact_direct_worker_script_is_supported(tmp_path, worker_core, python, variant):
    from hermes_cli import _launch_template
    core = worker_core
    script = _launch_template.render(str(core), 'agent.managed_worker', 'main')
    if variant == 'changed-tail':
        script += '\n'
    elif variant == 'arbitrary-module':
        script = _launch_template.render(str(core), 'attest_probe', 'main')
    else:
        script = _launch_template.relaunch_prefix(str(core), ['-c']) + 'exec(' + repr(script) + ')'
    # The refusal is observational: the fixture's final-dispatch probe still runs.
    probe = helpers.run_stub(tmp_path, python, script, args=())
    helpers.refused(probe, 'unsupported-route:stub-tail')
