"""The no_local_session warning stays once per (chat, entry) across maintenance rechecks.

Regression guard for PR #4589: once absence rechecks were gated on maintenance
admission, the A1 "warning once" check no longer re-entered the lookup, so a
warning logged on every recheck went unnoticed.
"""
import logging

from tests.tui_gateway import test_remote_change_absence as absence
from tui_gateway import notice_fanout as nf
from tui_gateway.unloaded_diagnostics import Admission

remote = absence.remote
absent = absence.absent


def _warnings(caplog):
    return [r for r in caplog.records if r.name == nf.__name__ and r.levelno == logging.WARNING
            and 'no_local_session' in r.getMessage()]


def test_absent_warning_once_across_maintenance_rechecks(absent, caplog):
    with caplog.at_level(logging.WARNING, logger=nf.__name__):
        assert not absent['run']().incomplete
        for _ in range(3):
            absent['clock'][0] += 60
            with Admission(absent['clock'][0], maintenance=True):
                assert not absent['run']().incomplete
    # Each maintenance pass really re-entered the absent chat's lookup ...
    assert absent['opened'] == ['missing'] * 4
    # ... yet the unchanged absence is reported once, not on every recheck.
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert 'missing' in warnings[0].getMessage()


def test_absent_warning_repeats_for_new_entry(absent, caplog):
    with caplog.at_level(logging.WARNING, logger=nf.__name__):
        absent['run']()
        absent['remote']['cards'][0]['status'] = 'running'
        nf._remote_memo = None
        absent['run']()
    # A changed card set is a new absence fact and is reported again.
    assert len(_warnings(caplog)) == 2
