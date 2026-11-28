"""Actual SCM_RIGHTS gate handoff; an ACK is not the retirement authority."""
from pathlib import Path

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_retirement as retirement
from hermes_cli.serve_producer_census import LinuxProcessCensus
from hermes_cli.serve_producer_mode import write_mode
from hermes_cli.serve_producer_participation import retired_participant

pytestmark = pytest.mark.platforms('linux')
ROOT = Path(__file__).parents[2]


def setup_server(tmp_path, drain):
    runtime = tmp_path / 'runtime'
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    server = retirement.bind_panel(runtime, tmp_path, drain)
    return runtime, ticket, server


def test_supported_panel_positive_drain_lends_same_gate_and_publishes_kernel_witness(tmp_path):
    calls = []
    runtime, ticket, server = setup_server(tmp_path, lambda: calls.append('all lanes joined') or True)
    write_mode(tmp_path, external=True)
    adapter = LinuxProcessCensus()
    record = adapter.read_process(ticket.identity.pid, adapter.boot_id())
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            retirement.request_retirement(runtime, ticket, tmp_path, gate)
            assert retired_participant(record, (ticket,), runtime, ticket.generation, str(ROOT))
            # The control server closes only its lent description; the collecting lock is held.
            with pytest.raises(admission.CustodyConflict):
                admission.LaunchGate.acquire(runtime)
        assert calls == ['all lanes joined']
    finally:
        server.close()


def test_failed_all_lane_drain_does_not_publish_witness(tmp_path):
    runtime, ticket, server = setup_server(tmp_path, lambda: False)
    write_mode(tmp_path, external=True)
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            with pytest.raises(admission.AdmissionUnavailable, match='not positively retired'):
                retirement.request_retirement(runtime, ticket, tmp_path, gate)
        assert server.participant._witness is None
    finally:
        server.close()


def test_off_mode_request_cannot_retire_sources(tmp_path):
    calls = []
    runtime, ticket, server = setup_server(tmp_path, lambda: calls.append(True) or True)
    try:
        with admission.LaunchGate.acquire(runtime) as gate:
            with pytest.raises((ValueError, OSError)):
                retirement.request_retirement(runtime, ticket, tmp_path, gate)
        assert calls == [] and server.participant._witness is None
    finally:
        server.close()


def test_unenrolled_or_unbound_launch_cannot_implicitly_attest_empty_panel(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    with pytest.raises((OSError, admission.AdmissionUnavailable, admission.CensusUnavailable)):
        retirement.bind_panel(runtime, tmp_path, lambda: True)
    ticket = admission.enroll_launch(runtime, 'serve', installation=ROOT)
    write_mode(tmp_path, external=True)
    with admission.LaunchGate.acquire(runtime) as gate:
        with pytest.raises(FileNotFoundError):
            retirement.request_retirement(runtime, ticket, tmp_path, gate)


def test_rebind_preserves_custody_and_uses_latest_drain_callback(tmp_path):
    runtime, ticket, server = setup_server(tmp_path, lambda: False)
    write_mode(tmp_path, external=True)
    try:
        same = retirement.bind_panel(runtime, tmp_path, lambda: True)
        assert same is server
        with admission.LaunchGate.acquire(runtime) as gate:
            retirement.request_retirement(runtime, ticket, tmp_path, gate)
        assert server.participant._witness is not None
    finally:
        server.close()
