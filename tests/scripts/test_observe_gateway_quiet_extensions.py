"""Extension diagnostics do not subtract CPU or excuse activity/identity changes."""
import dataclasses
from pathlib import Path

import pytest

from tests.scripts import test_observe_gateway_quiet as helpers

obs = helpers.obs
REPORT = {'count': 2, 'sha256': 'a' * 64, 'entries': ['native-load:/fixture.so', 'unrooted-module:fixture']}


@pytest.mark.parametrize('case,expected', [
    ('clean', obs.PASS), ('cpu', obs.FAIL_THRESHOLD), ('work', obs.INVALID), ('pid', obs.INVALID),
])
def test_report_survives_verdict_without_weakening_measurement(case, expected):
    samples = [dataclasses.replace(s, snapshot={**s.snapshot, 'unattested_code': REPORT})
               for s in helpers.run_of()]
    if case == 'cpu':
        samples[-1] = dataclasses.replace(samples[-1], cpu_s=samples[0].cpu_s + 31)
    if case == 'work':
        samples[20].snapshot['active_count'] = 1
    if case == 'pid':
        samples[20] = dataclasses.replace(samples[20], pid_start='different-process')
    verdict = obs.evaluate(samples, helpers.CFG, helpers.EXPECTED)
    assert verdict.state == expected
    run = obs._Run(helpers.CFG, obs.Hooks(query=lambda *a, **k: None), Path('.'),
                   expected=helpers.EXPECTED)
    evidence = obs._finish(run, (0, 0), verdict, samples=samples)
    assert evidence['unattested_code'] == REPORT
    assert evidence['cpu']['overhead_subtracted'] is False
    if case == 'clean':
        assert evidence['message'] == 'extension code not attested (2 units); its CPU is in the numerator'
    else:
        assert evidence['message'] is None
