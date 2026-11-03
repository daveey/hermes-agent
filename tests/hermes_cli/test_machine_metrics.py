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
    assert set(out) <= {"cpu_load1", "cpus", "mem_used_pct", "disk_free_gb", "disk_total_gb", "active_card_runs",
                        "drain_active", "drain_until_ms"}
    assert out["active_card_runs"] is None or out["active_card_runs"] >= 0
    assert out.get("drain_active") in (None, 0, 1)  # omitted when the hold marker cannot be read
    assert "drain_until_ms" not in out or out.get("drain_active") == 1


def test_reporter_sends_at_most_every_10s_and_backs_off_on_old_board():
    sent, now = [], [1000.0]
    r = mm.Reporter(sent.append, collect_fn=lambda: {"cpus": 4}, clock=lambda: now[0])
    assert r.report_due() and sent == [{"cpus": 4}]
    now[0] += 9.999
    assert not r.report_due() and len(sent) == 1
    now[0] += 0.001
    assert r.report_due() and len(sent) == 2
    assert mm.REPORT_EVERY_S == 10.0

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
    monkeypatch.setattr(ksc, "reconcile_links", lambda boards, gw, **kwargs: None)
    monkeypatch.setattr(ksc, "wait_for_work", lambda *a, **k: "period")
    monkeypatch.setattr(mm, "collect", lambda disk_path=None: {"cpus": 2})
    rounds = iter([True, True, True, False])
    ksc.run_forever(lambda: next(rounds), interval=1)
    assert reported == [{"cpus": 2}]  # three rounds inside one 10 s window


CPU = {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1_700_000_000_000}
CPU_KEYS = set(CPU)


def cpu_reporter(send, *, limit=("on", 50.0), cpu=lambda: dict(CPU), calls=None, now=None):
    calls = [] if calls is None else calls
    now = [1000.0] if now is None else now

    def collect():
        calls.append("collect")
        return {"cpus": 4, "drain_active": 0}

    def sample():
        calls.append("cpu")
        return cpu()
    return mm.Reporter(send, collect_fn=collect, clock=lambda: now[0], cpu_limit_fn=lambda: limit,
                       cpu_sample_fn=sample)


def test_cpu_triple_only_when_root_gate_configured_and_sampled_last():
    sent, calls = [], []
    assert cpu_reporter(sent.append, calls=calls).report_due()
    assert sent == [{"cpus": 4, "drain_active": 0, **CPU}] and calls == ["collect", "cpu"]
    for limit in (("off", None), ("unknown", None)):
        sent, calls = [], []
        assert cpu_reporter(sent.append, limit=limit, calls=calls).report_due()
        assert sent == [{"cpus": 4, "drain_active": 0}] and calls == ["collect"]  # never samples off-hub


def test_incomplete_cpu_measurement_is_omitted_never_coerced_to_idle():
    sent = []
    assert cpu_reporter(sent.append, cpu=lambda: None).report_due()
    assert not CPU_KEYS & set(sent[0])


def test_old_board_fallback_strips_cpu_and_drain_fields_once():
    sent = []

    def send(value):
        sent.append(value)
        if CPU_KEYS & set(value) or "drain_active" in value:
            raise ksc.HubError(400, "unknown metric")
    assert cpu_reporter(send).report_due()
    assert sent[1] == {"cpus": 4} and len(sent) == 2  # the retry never carries the (now older) CPU sample


def test_failed_cpu_report_is_never_replayed_next_round_takes_a_fresh_sample():
    sent, now, n = [], [1000.0], [0]

    def cpu():
        n[0] += 1
        return {**CPU, "cpu_busy_sample_end_ms": CPU["cpu_busy_sample_end_ms"] + n[0]}

    def send(value):
        sent.append(value)
        raise TimeoutError("CPU metrics report exceeded its 2 s budget")
    r = cpu_reporter(send, cpu=cpu, now=now)
    assert r.report_due() is False and len(sent) == 1  # a timeout is not retried in-round
    now[0] += mm.REPORT_EVERY_S
    assert r.report_due() is False and len(sent) == 2
    assert sent[0]["cpu_busy_sample_end_ms"] != sent[1]["cpu_busy_sample_end_ms"]


def test_worker_load_fields_unchanged_by_cpu_gate(tmp_path):
    out = mm.collect(disk_path=tmp_path)
    assert not CPU_KEYS & set(out)  # collect() never blocks on a CPU sample; only the hub reporter adds it


def test_active_card_runs_absent_default_board_counts_named_but_vanished_named_board_fails_closed(tmp_path, monkeypatch):
    """A never-created default board holds no runs (it must not blind the hub cap gate); a listed named board that
    cannot be read still makes the whole-host count unknown."""
    import sqlite3

    from hermes_cli import kanban_db_boards as kbb

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    named = tmp_path / "kanban" / "boards" / "proj" / "kanban.db"
    named.parent.mkdir(parents=True)
    db = sqlite3.connect(named)
    db.executescript("CREATE TABLE tasks (id TEXT, status TEXT); INSERT INTO tasks VALUES ('a','running'),('b','ready');")
    db.commit()
    db.close()
    assert not (tmp_path / "kanban.db").exists()
    assert mm.active_card_runs() == 1
    gone = tmp_path / "kanban" / "boards" / "gone" / "kanban.db"
    monkeypatch.setattr(kbb, "named_board_db_files", lambda: [("proj", named), ("gone", gone)])
    assert mm.active_card_runs() is None
