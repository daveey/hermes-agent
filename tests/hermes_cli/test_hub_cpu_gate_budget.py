"""Hub CPU gate operating budget and configuration surface (review F4, F8).

The lock-free advisory refuses every hub claim when the reporter's latest sample is older than 15 s (fail-closed,
intended): so a healthy sync loop must refresh it well inside that age even in its slowest round, a Board outage
must not starve it, and the root key must be a known, documented, off-by-default configuration key.
"""
import time

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm


def test_slowest_reporter_round_refreshes_the_advisory_inside_its_freshness_window():
    # Cadence + the longest valid sample + the hard POST budget: the next record lands before the 15 s limit.
    slowest_s = mm.REPORT_EVERY_S + cm.MAX_WINDOW_MS / 1000 + ksc.CPU_REPORT_BUDGET_S
    assert slowest_s < ka.ADVISORY_MAX_AGE_MS / 1000, slowest_s


def test_advisory_is_recorded_before_the_send_so_a_board_outage_does_not_starve_local_claims(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    end = int(time.time() * 1000)

    def send(_sample):
        raise TimeoutError("Board unreachable")
    reporter = mm.Reporter(send, collect_fn=lambda: {"cpus": 4}, cpu_limit_fn=lambda: (cm.ON, 50.0),
                           cpu_sample_fn=lambda: {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000,
                                                  "cpu_busy_sample_end_ms": end})
    assert reporter.report_due() is False  # the send failed and nothing raised into the sync loop
    assert mm.read_hub_cpu_sample(tmp_path) == {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000,
                                               "cpu_busy_sample_end_ms": end}


def test_root_cpu_gate_key_is_known_documented_and_off_by_default():
    from hermes_cli.config import _validate_config_key
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert _validate_config_key("kanban.admission.cpu_busy_below_pct") == (True, None)
    assert DEFAULT_CONFIG["kanban"]["admission"] == {"cpu_busy_below_pct": None}
    # The seeded null reads as "off": the gate never turns on from defaults.
    assert cm.root_limit(read_doc=lambda _p: {"kanban": DEFAULT_CONFIG["kanban"]}) == (cm.OFF, None)
