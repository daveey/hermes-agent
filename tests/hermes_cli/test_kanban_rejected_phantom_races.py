"""Retirement provenance races retain both tables and both operation orders."""
import pytest

from tests.hermes_cli import test_kanban_rejected_phantom_event as cases

svc = cases.svc
gws = cases.gws
refused = cases.refused


@pytest.mark.parametrize('target_first', [True, False])
@pytest.mark.parametrize('race', ['audit', 'mode', 'tombstone'])
def test_evidence_read_revalidates_retirement_provenance(refused, svc, monkeypatch, race, target_first):
    cases.assert_evidence_read_revalidates_retirement_provenance(refused, svc, monkeypatch, race, target_first)
