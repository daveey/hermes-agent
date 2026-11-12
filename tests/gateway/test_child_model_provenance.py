"""Child model provenance is diagnostic data, never worker authority over lineage or routing."""
from functools import partial

import pytest

import hermes_state_runtime as rt
from gateway.session_worker_child import reserve_child_execution
from tests.hermes_state import test_runtime_worker_children as children

ledger = children.ledger


@pytest.mark.parametrize("provenance", [
    None,
    "caller_pin",
    {"model_source": "made_up", "requested_model": "class:standard"},
    {"model_source": "caller_pin", "requested_model": 42},
    {"model_source": "caller_pin"},
    {"model_source": "caller_pin", "requested_model": "class:standard", "_delegate_from": "other"},
])
def test_reservation_refuses_malformed_or_authority_bearing_provenance(ledger, provenance):
    db, epoch = ledger
    handler = partial(reserve_child_execution, epoch=epoch, execution_id="worker",
                      identity={}, principal="local", limits=(2, 1), refused=None)
    with pytest.raises(rt.RuntimeStoreError, match="^invalid_params$"):
        rt.apply_worker_receipt(db, epoch=epoch, execution_id="worker", session_id="parent", generation=0,
                                sequence=1, operation="child.reserve",
                                payload={"secret": "synthetic-child-secret", "model_provenance": provenance},
                                handlers={"child.reserve": handler})
    with db._read_ctx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE source='subagent'").fetchone()[0] == 0
