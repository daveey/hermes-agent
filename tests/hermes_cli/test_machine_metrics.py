"""Machine load reporting for the Board registry: readers per platform, the 30 s reporter, the sync-loop hook."""
from pathlib import Path

from hermes_cli import kanban_sync_client as ksc
from hermes_cli import machine_drain_metrics as md
from hermes_cli import machine_metrics as mm
from hermes_cli import machine_throughput_metrics as mt

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
                        "drain_active", "drain_until_ms", "claiming", "tokens_by_model",
                        "persistent_jobs", "persistent_jobs_at_ms", *mt.FIELDS}
    assert out["active_card_runs"] is None or out["active_card_runs"] >= 0
    assert out.get("drain_active") in (None, 0, 1)  # omitted when the hold marker cannot be read
    assert out.get("claiming") in (0, 1)
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

    def take(_limit):
        calls.append("cpu")
        return cpu()
    return mm.Reporter(send, collect_fn=collect, clock=lambda: now[0], cpu_limit_fn=lambda: limit,
                       cpu_take_fn=take)


def finish_report(reporter, now):
    return reporter.report_due()  # the sampler thread measured already: one call reports


def test_cpu_triple_only_when_root_gate_configured_and_sampled_last():
    sent, calls = [], []
    now = [1000.0]
    assert finish_report(cpu_reporter(sent.append, calls=calls, now=now), now)
    assert sent == [{"cpus": 4, "drain_active": 0, **CPU}] and calls == ["collect", "cpu"]
    for limit in (("off", None), ("unknown", None)):
        sent, calls = [], []
        assert cpu_reporter(sent.append, limit=limit, calls=calls).report_due()
        assert sent == [{"cpus": 4, "drain_active": 0}] and calls == ["collect"]  # never samples off-hub


def test_incomplete_cpu_measurement_is_omitted_never_coerced_to_idle():
    sent = []
    now = [1000.0]
    assert finish_report(cpu_reporter(sent.append, cpu=lambda: None, now=now), now)
    assert not CPU_KEYS & set(sent[0])


def test_old_board_fallback_strips_cpu_and_drain_fields_once():
    sent = []

    def send(value):
        sent.append(value)
        if CPU_KEYS & set(value) or "drain_active" in value:
            raise ksc.HubError(400, "unknown metric")
    now = [1000.0]
    assert finish_report(cpu_reporter(send, now=now), now)
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
    assert finish_report(r, now) is False and len(sent) == 1  # a timeout is not retried in-round
    now[0] = 1000.0 + mm.REPORT_EVERY_S
    assert finish_report(r, now) is False and len(sent) == 2
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


# --- throughput counters -------------------------------------------------------------------------------------------


def _state_db(path: Path, sessions):
    """A real ``state.db`` (production schema via SessionDB); ``sessions`` = [(id, io_in, io_out, cr, cw, ended)]."""
    from hermes_state import SessionDB

    path.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=path)
    try:
        for sid, tin, tout, cread, cwrite, ended in sessions:
            db.create_session(sid, "cli", model="m")
            db.update_token_counts(sid, input_tokens=tin, output_tokens=tout, cache_read_tokens=cread,
                                   cache_write_tokens=cwrite, reasoning_tokens=1000, model="m", source="cli")
            if ended:
                db.end_session(sid, "done")
    finally:
        db.close()
    return path


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    _state_db(root / "state.db", [("a", 10, 5, 100, 1, True), ("b", 20, 7, 200, 2, False)])
    _state_db(root / "profiles" / "coach" / "state.db", [("c", 1, 2, 3, 4, True)])
    return root


def test_throughput_counters_sum_root_and_profiles_from_real_state_dbs(tmp_path):
    root = _root(tmp_path)
    assert mt.counters(root) == {"tokens_io_total": 45, "tokens_cache_total": 310,  # reasoning is not counted
                                 "sessions_started_total": 3, "sessions_ended_total": 2,
                                 "tokens_by_model": {"m": [45, 310]}}


def test_throughput_tokens_count_auxiliary_usage_rows(tmp_path):
    """``session_model_usage`` carries auxiliary calls (compression ...) that ``sessions`` never totals."""
    import sqlite3

    root = _root(tmp_path)
    db = sqlite3.connect(root / "state.db")
    db.execute("INSERT INTO session_model_usage (session_id, model, task, input_tokens, output_tokens, "
               "cache_read_tokens) VALUES ('a', 'aux', 'compression', 1000, 50, 7)")
    db.commit()
    db.close()
    out = mt.counters(root)
    assert out["tokens_io_total"] == 45 + 1050 and out["tokens_cache_total"] == 317


def test_throughput_counters_omitted_when_any_db_is_unreadable_never_zero(tmp_path):
    root = _root(tmp_path)
    (root / "profiles" / "broken").mkdir()
    (root / "profiles" / "broken" / "state.db").write_bytes(b"not a sqlite database" * 100)
    assert mt.counters(root) == {}
    assert mt.counters(tmp_path / "empty") == {}  # no state.db at all: unknown, not 0


def test_throughput_counters_cached_for_60s(tmp_path):
    root, now = _root(tmp_path), [100.0]
    mt.reset_cache()
    try:
        first = mt.cached_counters(root, clock=lambda: now[0])
        _state_db(root / "state.db", [("d", 1, 1, 0, 0, False)])
        now[0] += mt.CACHE_TTL_S - 0.1
        assert mt.cached_counters(root, clock=lambda: now[0]) == first
        now[0] += 0.1
        assert mt.cached_counters(root, clock=lambda: now[0])["sessions_started_total"] == 4
    finally:
        mt.reset_cache()
    assert mt.CACHE_TTL_S == 60.0


def test_throughput_counters_stay_cheap_on_a_hub_sized_state_db(tmp_path):
    """Timing guard: the hub's root state.db holds ~22k sessions; one uncached read must stay well under 1 s."""
    import sqlite3
    import time

    root = tmp_path / "hermes"
    _state_db(root / "state.db", [])
    db = sqlite3.connect(root / "state.db")
    db.executemany("INSERT INTO sessions (id, source, started_at, ended_at, input_tokens, output_tokens) "
                   "VALUES (?, 'cli', 1, ?, 5, 5)", ((f"s{i}", 2 if i % 10 else None) for i in range(22_000)))
    db.executemany("INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens) "
                   "VALUES (?, 'm', 5, 5)", ((f"s{i}",) for i in range(22_000)))
    db.commit()
    db.close()
    start = time.perf_counter()
    out = mt.counters(root)
    elapsed = time.perf_counter() - start
    assert out["sessions_started_total"] == 22_000 and out["sessions_ended_total"] == 19_800
    assert out["tokens_io_total"] == 220_000
    assert elapsed < 0.5, f"throughput counters took {elapsed:.3f}s on 22k sessions"


def test_collect_attaches_cached_throughput_counters(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "cached_counters", lambda: {"tokens_io_total": 7})
    assert mm.collect(disk_path=tmp_path)["tokens_io_total"] == 7


COUNTERS = {"tokens_io_total": 45, "tokens_cache_total": 310, "sessions_started_total": 3, "sessions_ended_total": 2}


def test_rejected_claiming_backs_off_so_the_next_report_keeps_cpu():
    """A Board that accepts CPU and rejects claiming must store a fresh CPU sample on the next report."""
    sent, now = [], [1000.0]
    cpu = {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1}

    def board(value):
        sent.append(dict(value))
        if "claiming" in value:
            raise ksc.HubError(400, "unknown field(s): claiming")

    r = mm.Reporter(board, collect_fn=lambda: {"cpus": 4, "drain_active": 0, "claiming": 1, **cpu},
                    clock=lambda: now[0])
    assert r.report_due()
    assert "cpu_busy_pct" in sent[0] and "claiming" in sent[0]
    assert "cpu_busy_pct" not in sent[1] and "claiming" not in sent[1]
    assert r.claiming_off_until == now[0] + mm.UNSUPPORTED_RETRY_S
    now[0] += mm.REPORT_EVERY_S
    assert r.report_due() and len(sent) == 3
    assert sent[2]["cpu_busy_pct"] == 12.5 and "claiming" not in sent[2]
    now[0] = 1000.0 + mm.UNSUPPORTED_RETRY_S
    assert r.report_due() and sent[3]["claiming"] == 1


def test_board_without_counters_gets_the_normal_report_and_is_not_asked_again_for_600s():
    sent, now = [], [1000.0]
    sample = {"cpus": 4, "drain_active": 0, "claiming": 1, **COUNTERS}

    def old_board(value):
        sent.append(value)
        if set(COUNTERS) & set(value):
            raise ksc.HubError(400, '{"error":"unknown field(s): tokens_io_total, tokens_cache_total"}')
    r = mm.Reporter(old_board, collect_fn=lambda: dict(sample), clock=lambda: now[0])
    assert r.report_due()
    without_claiming = {k: v for k, v in sample.items() if k != "claiming"}
    assert sent == [sample, without_claiming, {"cpus": 4, "drain_active": 0}]
    assert r.counters_off_until == now[0] + mm.UNSUPPORTED_RETRY_S
    assert r.claiming_off_until == now[0] + mm.UNSUPPORTED_RETRY_S
    now[0] += mm.REPORT_EVERY_S
    assert r.report_due() and sent[3:] == [{"cpus": 4, "drain_active": 0}]
    now[0] = 1000.0 + mm.UNSUPPORTED_RETRY_S
    assert r.report_due() and sent[4] == sample  # asks again after the backoff, claiming included


def test_counter_retry_drops_cpu_sample_and_still_falls_back_for_pre_drain_boards():
    sent = []
    sample = {"cpus": 4, "drain_active": 0, "claiming": 1, **CPU, **COUNTERS}

    def ancient(value):
        sent.append(value)
        if set(value) - {"cpus"}:
            raise ksc.HubError(400, "unknown field(s): drain_active")
    md.send_compatible(ancient, sample)
    assert "claiming" not in sent[1] and "cpu_busy_pct" not in sent[1]
    assert sent[1]["tokens_io_total"] == COUNTERS["tokens_io_total"]
    assert sent[2] == {"cpus": 4, "drain_active": 0} and sent[3:] == [{"cpus": 4}]


def test_other_400_with_counters_is_not_treated_as_an_old_board():
    sent = []

    def picky(value):
        sent.append(value)
        raise ksc.HubError(400, "tokens_io_total must be a non-negative integer")
    r = mm.Reporter(picky, collect_fn=lambda: {"cpus": 4, **COUNTERS})
    assert r.report_due() is False and len(sent) == 1 and r.counters_off_until == 0.0
