"""A reading with no reset still spends its usage: sustainable = (target - used) / window (t_01a1dd5a1d4c2379). [daveey]

Post-merge audit of PR #3058 (t_01a1dd123a28b4da): replacing ``max(0.0, cfg.target - used) / WINDOW_SECONDS[kind]``
by ``cfg.target / WINDOW_SECONDS[kind]`` in ``_sustainable`` survived every pacing test, because each fixture whose
reading has ``reset_at=None`` used 0%. A no-reset window that is already 40% used then looked like a fresh full window
and the pool factor was overestimated. Expected values below are hand-derived percent-per-hour arithmetic, not
recomputed with the production formula.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_llm_pacing as lp

HOUR, DAY = 3600.0, 86400.0
NOW = 1_800_000_000.0
# Every account stays a headroom account (reserve above 100%), so a no-reset reading near/over target still counts.
CFG = lp.PacingConfig(reserve={lp.FIVE_HOUR: 101.0, lp.SEVEN_DAY: 101.0})


def _pool(kind, burner_used, burner_before, burner_reset_in, idle_used):
    """``burner`` has a reset and a measured burn; ``idle`` has ``idle_used`` percent and NO reset, so it burns 0."""
    current = [lp.Reading(NOW, "burner", kind, burner_used, NOW + burner_reset_in),
               lp.Reading(NOW, "idle", kind, idle_used, None)]
    history = [lp.Reading(NOW - HOUR, "burner", kind, burner_before, NOW + burner_reset_in)]
    return lp.pool_pace("anthropic", current, history, CFG)


# five_hour: burner 60% used, 1 h to reset, 30 points over the last hour -> burn 30 %/h and sustainable
# (95-60)/1h = 35 %/h. The no-reset account adds (95-used)/5h: 55/5 = 11 %/h at 40%, 0.2 %/h at 94%.
# factor = (35 + idle %/h) / 30.
@pytest.mark.parametrize("idle_used, expected", [
    (0, (35 + 19) / 30),       # 1.8: a fresh window adds the full 95/5h = 19 %/h (the old behaviour, kept)
    (40, (35 + 11) / 30),      # 1.5333: nonzero usage must be subtracted; the mutant gives 1.8
    (94, (35 + 0.2) / 30),     # 1.1733: just under target
    (95, 35 / 30),             # 1.1667: exactly at target adds nothing
    (99, 35 / 30),             # 1.1667: past target clamps at zero, never subtracts from the others
])
def test_no_reset_five_hour_window_subtracts_its_usage(idle_used, expected):
    pace = _pool(lp.FIVE_HOUR, 60, 30, HOUR, idle_used)
    assert pace.factor == pytest.approx(round(expected, 4), abs=1e-4)
    assert pace.state == lp.OPEN


def test_no_reset_seven_day_window_subtracts_its_usage():
    # burner: 30%, 3 days to reset, +3 points over the last hour -> burn 3 %/h; sustainable 65/72h = 0.9028 %/h.
    # idle: 50% used, no reset -> 45/(7*24h) = 0.26786 %/h (a fresh window would add 95/168 = 0.5655 %/h).
    pace = _pool(lp.SEVEN_DAY, 30, 27, 3 * DAY, 50)
    assert pace.factor == pytest.approx((65 / 72 + 45 / 168) / 3, abs=1e-4)  # 0.3902; the mutant gives 0.4894
    assert pace.factor == pytest.approx(0.3902, abs=1e-4)
    assert pace.state == lp.PACED


def test_no_reset_usage_decides_paced_versus_open():
    """burner 60% (was 15% an hour ago, 1 h to reset): burn 45 %/h, sustainable 35 %/h. The no-reset account at
    75% adds 20/5h = 4 %/h -> 39/45 = 0.8667: PACED. Treated as a fresh window (19 %/h) it would read 54/45 = 1.2 OPEN."""
    pace = _pool(lp.FIVE_HOUR, 60, 15, HOUR, 75)
    assert pace.state == lp.PACED
    assert pace.factor == pytest.approx(0.8667, abs=1e-4)


def test_no_reset_account_over_target_does_not_cancel_other_headroom():
    """99% used with no reset: (95-99) must clamp to 0, not subtract 4/5h = 0.8 %/h from the burner's 35 %/h."""
    pace = _pool(lp.FIVE_HOUR, 60, 30, HOUR, 99)
    assert pace.factor == pytest.approx(round(35 / 30, 4), abs=1e-4)  # unclamped would be (35-0.8)/30 = 1.14


def test_controls_a_reset_window_and_a_reserved_account_are_unchanged():
    # Ordinary reset control: 60% with 1 h to reset -> (95-60)/1h = 35 %/h, no fallback to the full window.
    current = [lp.Reading(NOW, "burner", lp.FIVE_HOUR, 60, NOW + HOUR)]
    history = [lp.Reading(NOW - HOUR, "burner", lp.FIVE_HOUR, 30, NOW + HOUR)]
    assert lp.pool_pace("anthropic", current, history, CFG).factor == pytest.approx(35 / 30, abs=1e-4)

    # Reserved control (default reserve 80%): the 40%-used no-reset account is headroom, the 85% one is not.
    default = lp.PacingConfig()
    current = [lp.Reading(NOW, "burner", lp.FIVE_HOUR, 60, NOW + HOUR),
               lp.Reading(NOW, "idle", lp.FIVE_HOUR, 40, None),
               lp.Reading(NOW, "spent", lp.FIVE_HOUR, 85, None)]
    pace = lp.pool_pace("anthropic", current, history, default)
    assert pace.factor == pytest.approx((35 + 11) / 30, abs=1e-4)  # "spent" adds nothing
