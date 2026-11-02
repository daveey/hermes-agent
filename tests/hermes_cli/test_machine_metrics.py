"""Machine load reporting for the Board registry: readers per platform, the 30 s reporter, the sync-loop hook."""
from pathlib import Path

from hermes_cli import kanban_sync_client as ksc
from hermes_cli import machine_metrics as mm

MEMINFO = "MemTotal:       65000000 kB\nMemFree:         1000000 kB\nMemAvailable:   13000000 kB\n"
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               65536.
Pages active:                            500000.
Pages inactive:                          131072.
Pages speculative:                        65536.
Pages purgeable:                              0.
"""


def test_linux_memory_reader():
    assert mm._linux_mem_used_pct(MEMINFO) == 80.0
    assert mm._linux_mem_used_pct("MemTotal: 1 kB\n") is None


def test_macos_memory_reader():
    memsize = 16 * 1024 ** 3  # 16 GB; available = (65536+131072+65536) pages * 16 KB = 4 GB
    assert mm._macos_mem_used_pct(memsize, VM_STAT) == 75.0
    assert mm._macos_mem_used_pct(memsize, "no page size here") is None


def test_collect_reads_this_machine(tmp_path: Path):
    out = mm.collect(disk_path=tmp_path)
    assert out["cpus"] >= 1 and out["cpu_load1"] >= 0
    assert 0 < out["disk_free_gb"] <= out["disk_total_gb"]
    assert 0 <= out["mem_used_pct"] <= 100  # this platform's reader (linux or macos)
    assert set(out) <= {"cpu_load1", "cpus", "mem_used_pct", "disk_free_gb", "disk_total_gb", "active_card_runs"}
    assert out["active_card_runs"] is None or out["active_card_runs"] >= 0


def test_reporter_sends_at_most_every_30s_and_backs_off_on_old_board():
    sent, now = [], [1000.0]
    r = mm.Reporter(sent.append, collect_fn=lambda: {"cpus": 4}, clock=lambda: now[0])
    assert r.report_due() and sent == [{"cpus": 4}]
    now[0] += 29
    assert not r.report_due() and len(sent) == 1
    now[0] += 1
    assert r.report_due() and len(sent) == 2

    def old_board(_m):
        raise ksc.HubError(404, "not found")
    r = mm.Reporter(old_board, collect_fn=dict, clock=lambda: now[0])
    assert r.report_due() is False  # never raises into the sync loop
    assert r.next_at == now[0] + mm.UNSUPPORTED_RETRY_S

    def flaky(_m):
        raise OSError("network down")
    r = mm.Reporter(flaky, collect_fn=dict, clock=lambda: now[0])
    assert r.report_due() is False
    assert r.next_at == now[0] + mm.REPORT_EVERY_S


def test_hub_report_metrics_posts_to_own_route(monkeypatch):
    calls = []
    hub = ksc.Hub(ksc.Identity(url="https://board.test", key="k" * 16, gateway="gw"))
    monkeypatch.setattr(hub, "_call", lambda method, path, body=None: calls.append((method, path, body)) or {})
    hub.report_metrics({"cpus": 8})
    assert calls == [("POST", "/v2/machines/me/metrics", {"cpus": 8})]


def test_sync_loop_reports_metrics_once_per_window(monkeypatch):
    reported = []
    monkeypatch.setattr(ksc, "load_identity", lambda: ksc.Identity(url="https://board.test", key="k" * 16, gateway="gw"))
    monkeypatch.setattr(ksc.Hub, "report_metrics", lambda self, m: reported.append(m))
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda c: None)
    monkeypatch.setattr(ksc, "board_paths", list)
    monkeypatch.setattr(ksc, "sync_all_once", lambda hub, *, boards=None: [])
    monkeypatch.setattr(ksc, "reconcile_links", lambda boards, gw: None)
    monkeypatch.setattr(ksc, "wait_for_work", lambda *a, **k: "period")
    monkeypatch.setattr(mm, "collect", lambda disk_path=None: {"cpus": 2})
    rounds = iter([True, True, True, False])
    ksc.run_forever(lambda: next(rounds), interval=1)
    assert reported == [{"cpus": 2}]  # three rounds inside one 30 s window
