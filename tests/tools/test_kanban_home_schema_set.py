"""Set-field storage parity; split to respect the per-file test budget."""
import pytest
from tests.tools.test_kanban_home_schema_contract import (
    PROPERTIES, assert_schema_sender_to_stored_owner_parity,
)
from tests.tools import test_kanban_home_schema_contract as contract

svc, gws, pool_labels = contract.svc, contract.gws, contract.pool_labels


@pytest.mark.parametrize('field', sorted(PROPERTIES))
def test_set(gws, monkeypatch, pool_labels, field):
    assert_schema_sender_to_stored_owner_parity(gws, monkeypatch, pool_labels, field, 'set')
