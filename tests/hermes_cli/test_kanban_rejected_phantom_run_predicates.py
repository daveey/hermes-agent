"""Run retirement predicates; real per-case gateways, unchanged file deadline."""
import pytest

from tests.hermes_cli import test_kanban_rejected_phantom_event as cases

svc = cases.svc
gws = cases.gws
refused = cases.refused


@pytest.mark.parametrize('refused', ['task_runs'], indirect=True)
@pytest.mark.parametrize('veto', cases.VETOES)
def test_every_retirement_predicate_fails_closed(refused, veto):
    cases.assert_retirement_predicate_fails_closed(refused, veto)
