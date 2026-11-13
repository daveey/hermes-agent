"""An expired remote state costs a transcript scan once, on its first settle, never on every pass. [daveey]

MacBook gateway, 2026-11-13 04:28-04:40 PST (t_01a1d4de28a8510d): ``collect`` asked ``transcript_has`` for every
card whose observed state was past ``LATE_MAX_S`` on every pass, before ``_claim_notice`` looked at the ledger. Two
busy chats carried 158 and 207 such cards, all already settled, so each owner pass spent 12-20 s in lineage
notice-key scans whose answer was never used, and the 5 s notifier loop it shares ran 13-88 s late.
"""
import logging
import sqlite3

from tests.tui_gateway.test_kanban_remote_late_observed import REF, T0, TID, _chat, _ledger_row, \
    _observed_then_starved
from tui_gateway import kanban_remote_notices as krn
from tests.tui_gateway.test_kanban_remote_notices import CHAT

GONE = T0 + 120 + 72 * 3600 + 600  # past the 72 h late window


class _Has:
    def __init__(self, answer: bool) -> None:
        self.answer, self.calls = answer, []

    def __call__(self, key: str, task_id: str) -> bool:
        self.calls.append((key, task_id))
        return self.answer


def test_expired_state_scans_the_transcript_once_then_never_again(tmp_path, caplog):
    ledger = tmp_path / "ledger.db"
    with _chat(tmp_path):
        replica = _observed_then_starved(tmp_path, ledger)
        has = _Has(False)
        with caplog.at_level(logging.INFO, logger=krn.logger.name):
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=GONE, transcript_has=has) == []
            assert len(has.calls) == 1  # the first settle chooses "transcript" vs "expired"
            for n in range(1, 6):  # later passes (the owner pass runs every few seconds), restarts included
                if n % 2:
                    krn._reset_for_tests()
                assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=GONE + 10 * n,
                                   transcript_has=has) == []
        assert len(has.calls) == 1, f"settled expired card rescanned {len(has.calls) - 1} more time(s)"
    row = _ledger_row(ledger, CHAT, REF)
    assert row["delivered"] == krn.SETTLED and row["settled"] == "expired"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == krn.logger.name]
    assert len(warnings) == 1 and TID in warnings[0].getMessage()


def test_expired_state_already_in_the_transcript_settles_transcript_without_a_warning(tmp_path, caplog):
    ledger = tmp_path / "ledger.db"
    with _chat(tmp_path):
        replica = _observed_then_starved(tmp_path, ledger)
        has = _Has(True)
        with caplog.at_level(logging.INFO, logger=krn.logger.name):
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=GONE, transcript_has=has) == []
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=GONE + 30,
                               transcript_has=has) == []
        assert len(has.calls) == 1
    row = _ledger_row(ledger, CHAT, REF)
    assert row["delivered"] == krn.SETTLED and row["settled"] == "transcript"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING and r.name == krn.logger.name]
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT reason FROM swept").fetchall() in ([], [("transcript",)])


def test_many_settled_expired_cards_cost_no_scan_per_pass(tmp_path):
    """The live shape: a long-lived chat with many expired cards, all settled. A pass scans none of them."""
    ledger = tmp_path / "ledger.db"
    with _chat(tmp_path):
        replica = _observed_then_starved(tmp_path, ledger)
        has = _Has(False)
        krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=GONE, transcript_has=has)
        has.calls.clear()
        for n in range(20):
            krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=GONE + 5 * (n + 1), transcript_has=has)
        assert has.calls == []
