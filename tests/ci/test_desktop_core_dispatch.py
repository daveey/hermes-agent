"""A scoped manual Linux run exercises Desktop specs masked in ordinary PR CI."""
from pathlib import Path

import hermes_yaml

ROOT = Path(__file__).resolve().parents[2]


def test_manual_desktop_core_uses_existing_isolated_linux_job():
    workflow = hermes_yaml.safe_load(
        (ROOT / '.github/workflows/e2e-desktop-core.yml').read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert 'workflow_dispatch' in triggers
    assert 'workflow_call' in triggers
    assert set(triggers) == {'workflow_dispatch', 'workflow_call'}
    assert workflow['permissions'] == {'contents': 'read'}
    assert set(workflow['jobs']) == {'core'}
    job = workflow['jobs']['core']
    assert job['runs-on'] == 'blacksmith-4vcpu-ubuntu-2404'
    checkout = job['steps'][0]
    assert checkout['with']['ref'] == '${{ inputs.commit }}'
    run = next(step for step in job['steps'] if step.get('name') == 'Run the core suite under xvfb')
    assert 'playwright test -c e2e/core/playwright.config.ts' in run['run']
    assert run['env']['HERMES_E2E_PYTHON'] == '${{ steps.pm.outputs.python-path }}'
    for key in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'NOUS_API_KEY', 'ANTHROPIC_API_KEY'):
        assert run['env'][key] == ''
