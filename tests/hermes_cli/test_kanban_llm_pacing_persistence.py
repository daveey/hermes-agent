"""Dispatch burn history must survive the process that sampled it. [daveey]"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SAMPLE = """
import json
import sys
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_llm_pacing_history as hist

now, used = map(int, sys.argv[1:])
# Only the external usage endpoint and the Board's token registry are replaced; persistence, reload, rate math,
# the capacity history and the in-process cache all use production code.
pd.read_pool = lambda provider, ts: [lp.Reading(ts, 'a', lp.SEVEN_DAY, used, 1800036000)]
hist.fleet_tokens = lambda provider: {'hub': float(used) * 1000}
assert not pd._CACHE
cfg = lp.PacingConfig()
pace = pd.pace_for(None, 'anthropic', cfg, now)
with hist.History.open(pd.history_path(), 'anthropic') as store:
    reads = len(store.recent_reads(now + 1, 9))
print(json.dumps({'pace': pace.as_dict(), 'reads': reads}))
"""


def test_dispatch_rate_history_survives_process_restart(tmp_path):
    home = tmp_path / 'home'
    state = home / '.hermes'
    state.mkdir(parents=True)
    env = {**os.environ, 'HOME': str(home), 'HERMES_HOME': str(state),
           'HERMES_KANBAN_HOME': str(state)}
    root = Path(__file__).resolve().parents[2]

    def sample(now, used):
        result = subprocess.run(
            [sys.executable, '-c', _SAMPLE, str(now), str(used)],
            cwd=root, env=env, capture_output=True, text=True, timeout=30, check=True,
        )
        return json.loads(result.stdout)

    # The week-average fallback is open. Only the saved 54 -> 60%/hour
    # measurement can make the next fresh interpreter pace this pool.
    first = sample(1799996400, 54)
    assert first['pace']['state'] == 'open' and first['reads'] == 1
    restarted = sample(1800000000, 60)
    assert restarted['pace']['state'] == 'paced'
    assert restarted['pace']['factor'] == pytest.approx(0.5833)
    assert restarted['reads'] == 2  # the capacity history survives the restart too
    data = json.loads((state / 'llm_pacing.json').read_text(encoding='utf-8-sig'))
    assert data['version'] == 2 and data['v2']['pools']['anthropic']['read_at'] == 1800000000
    saved = data['anthropic']
    assert saved['samples'] == [
        [1799996400, 'a', 'seven_day', 54, 1800036000],
        [1800000000, 'a', 'seven_day', 60, 1800036000],
    ]
    assert 'running' not in saved  # the per-host start limit's concurrency samples are gone
    assert saved['read_at'] == 1800000000
    assert saved['pace'] == restarted['pace']
