"""Hub CPU gate operating budget and configuration surface (review F4, F8).

The lock-free advisory refuses every hub claim when the sampler's latest sample is older than 15 s (fail-closed,
intended): so the advisory sampler must refresh it well inside that age whatever the sync loop's round time, a Board outage
must not starve it, and the root key must be a known, documented, off-by-default configuration key.
"""
from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_cpu_sampler as cs
from hermes_cli import machine_metrics as mm


BOARD_CPU_MAX_DELAY_MS = 2000  # hermes-hub worker/src/mailbox.ts CPU_MAX_DELAY_MS (deployed d9e950ade108)


def test_slowest_sampler_round_refreshes_the_advisory_inside_its_freshness_window():
    # The sampler's own write cadence + the longest valid window: the next record lands before the 15 s limit,
    # whatever the sync loop's round time (the sampler runs on its own thread).
    slowest_s = cs.record_every_s() + cm.MAX_WINDOW_MS / 1000
    assert slowest_s < ka.ADVISORY_MAX_AGE_MS / 1000, slowest_s
    # A report attaches a sample ending at most REPORT_MAX_AGE_MS before the take, which leaves the send part of the
    # Board's 2 s window; the advisory's 15 s freshness is unaffected by the report.
    assert cs.REPORT_MAX_AGE_MS < BOARD_CPU_MAX_DELAY_MS
    assert cs.REPORT_MAX_AGE_MS / 1000 + ksc.CPU_REPORT_BUDGET_S < ka.ADVISORY_MAX_AGE_MS / 1000


def test_advisory_is_recorded_without_any_send_so_a_board_outage_does_not_starve_local_claims(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    now = [100.0]
    stats = iter(["cpu 100 0 0 900 0 0 0 0\n", "cpu 150 0 0 1850 0 0 0 0\n"])  # 50 busy of 1000 ticks: 5 %
    sampler = cs.AdvisorySampler(limit_fn=lambda: (cm.ON, 50.0), read_fn=lambda: next(stats), clock=lambda: now[0],
                                 wall=lambda: 1_700_000_000 + now[0])
    assert sampler.run_once() is None  # the first snapshot
    now[0] += 1.0
    triple = {"cpu_busy_pct": 5.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1_700_000_101_000}
    assert sampler.run_once() == triple  # the sampler thread's step: no Board call at all
    assert mm.read_hub_cpu_sample(tmp_path) == triple

    def send(_sample):
        raise TimeoutError("Board unreachable")
    reporter = mm.Reporter(send, collect_fn=lambda: {"cpus": 4}, cpu_limit_fn=lambda: (cm.ON, 50.0),
                           cpu_take_fn=sampler.take)
    assert reporter.report_due() is False  # the send failed and nothing raised into the sync loop
    assert mm.read_hub_cpu_sample(tmp_path) == triple


def test_root_cpu_gate_key_is_known_documented_and_off_by_default():
    from hermes_cli.config import _validate_config_key
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert _validate_config_key("kanban.admission.cpu_busy_below_pct") == (True, None)
    assert DEFAULT_CONFIG["kanban"]["admission"] == {"cpu_busy_below_pct": None}
    # The seeded null reads as "off": the gate never turns on from defaults.
    assert cm.root_limit(read_doc=lambda _p: {"kanban": DEFAULT_CONFIG["kanban"]}) == (cm.OFF, None)
