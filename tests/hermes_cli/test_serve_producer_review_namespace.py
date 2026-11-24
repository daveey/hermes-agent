"""Review probe: namespace is part of a receipt's process identity."""
import json
from dataclasses import asdict, replace
from pathlib import Path
import pytest
from hermes_cli.serve_producer_admission import enroll_launch, read_participants_strict

pytestmark = pytest.mark.platforms('linux')

def test_foreign_same_numeric_identity_is_preserved(tmp_path):
    runtime = tmp_path / 'runtime'
    installation = Path(__file__).resolve().parents[2]
    local = enroll_launch(runtime, 'gateway', installation=installation)
    foreign = replace(local, namespace='pid:[1]')
    assert foreign.namespace != local.namespace
    ledger = runtime / 'participants.json'
    ledger.write_text(json.dumps([asdict(foreign)]))
    enroll_launch(runtime, 'gateway', installation=installation)
    rows = json.loads(ledger.read_text())
    assert asdict(foreign) in rows, 'live foreign namespace receipt was silently deleted'

def test_distinct_namespaces_can_share_numeric_identity(tmp_path):
    runtime = tmp_path / 'runtime'
    installation = Path(__file__).resolve().parents[2]
    local = enroll_launch(runtime, 'gateway', installation=installation)
    foreign = replace(local, namespace='pid:[1]')
    ledger = runtime / 'participants.json'
    ledger.write_text(json.dumps([asdict(local), asdict(foreign)]))
    assert len(read_participants_strict(runtime, allow_foreign_namespace=True)) == 2
