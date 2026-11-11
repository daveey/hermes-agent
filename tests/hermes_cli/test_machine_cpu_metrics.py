"""Hub CPU busy measurement: /proc/stat deltas, the bounded 1 s sampler and the strict <limit comparison."""
import math

import pytest

from hermes_cli import machine_cpu_metrics as cm


def stat(user=0, nice=0, system=0, idle=0, iowait=0, irq=0, softirq=0, steal=0, guest=0, guest_nice=0):
    return (f"cpu  {user} {nice} {system} {idle} {iowait} {irq} {softirq} {steal} {guest} {guest_nice}\n"
            "cpu0 1 2 3 4 5 6 7 8 9 10\nintr 12345\n")


def test_parse_sums_user_through_steal_and_never_adds_guest_twice():
    # guest/guest_nice are already inside user/nice: adding them would inflate busy.
    total, idle = cm.parse_proc_stat(stat(user=100, nice=10, system=20, idle=500, iowait=30, irq=5, softirq=5,
                                          steal=30, guest=90, guest_nice=9))
    assert total == 100 + 10 + 20 + 500 + 30 + 5 + 5 + 30
    assert idle == 500 + 30 + 10  # iowait and nice are not admission busy; steal is unavailable


@pytest.mark.parametrize("text", ["", "intr 1\n", "cpu  1 2 x 4\n", "cpu  1 2 3\n", "cpu  -1 2 3 4 5 6 7 8\n"])
def test_parse_malformed_is_unknown(text):
    assert cm.parse_proc_stat(text) is None


def test_busy_pct_definition_steal_counts_busy_iowait_idle():
    a = cm.parse_proc_stat(stat(user=100, idle=100))
    b = cm.parse_proc_stat(stat(user=120, idle=150, iowait=10, steal=20))  # dt=100, idle-like=60
    assert cm.busy_pct(a, b) == pytest.approx(40.0)


def test_busy_pct_counter_reset_and_zero_total_are_unknown():
    a = cm.parse_proc_stat(stat(user=100, idle=100))
    assert cm.busy_pct(a, a) is None  # zero total
    assert cm.busy_pct(a, cm.parse_proc_stat(stat(user=50, idle=200))) is None  # a counter went backwards (reboot)
    assert cm.busy_pct(None, a) is None and cm.busy_pct(a, None) is None


class FakeHost:
    def __init__(self, texts, elapsed_s=1.0, wall_ms=1_700_000_000_000):
        self.texts, self.elapsed_s, self.now, self.wall = list(texts), elapsed_s, 100.0, wall_ms / 1000

    def read(self):
        t = self.texts.pop(0)
        if isinstance(t, Exception):
            raise t
        return t

    def sleep(self, s):
        self.now += self.elapsed_s
        self.wall += self.elapsed_s

    def clock(self):
        return self.now

    def wall_clock(self):
        return self.wall


def sample(host, **kw):
    return cm.sample(read=host.read, clock=host.clock, wall=host.wall_clock, sleep=host.sleep, linux=True, **kw)


@pytest.mark.parametrize("nice,user,allowed", [(70, 10, True), (0, 80, False)])
def test_admission_uses_non_nice_busy(nice, user, allowed):
    out = sample(FakeHost([stat(), stat(user=user, nice=nice, idle=20)]))
    assert out is not None
    assert cm.below(out["cpu_busy_pct"], 50) is allowed


def test_sample_reports_complete_triple():
    host = FakeHost([stat(user=0, idle=0), stat(user=30, idle=70)])
    out = sample(host)
    assert out == {"cpu_busy_pct": pytest.approx(30.0), "cpu_busy_window_ms": 1000,
                   "cpu_busy_sample_end_ms": 1_700_000_001_000}


@pytest.mark.parametrize("elapsed", [0.999, 2.001, 5.0])
def test_sample_outside_1_to_2s_window_is_unknown(elapsed):
    host = FakeHost([stat(user=0, idle=0), stat(user=30, idle=70)], elapsed_s=elapsed)
    assert sample(host) is None


def test_sample_window_boundaries_accepted():
    for elapsed in (1.0, 2.0):
        assert sample(FakeHost([stat(idle=0), stat(user=1, idle=1)], elapsed_s=elapsed)) is not None


def test_sample_io_error_and_non_linux_are_unknown():
    assert sample(FakeHost([OSError("no /proc"), stat(idle=1)])) is None
    assert sample(FakeHost([stat(idle=0), OSError("gone")])) is None
    host = FakeHost([stat(idle=0), stat(user=1, idle=1)])
    assert cm.sample(read=host.read, clock=host.clock, wall=host.wall_clock, sleep=host.sleep, linux=False) is None


def test_sample_on_this_host_is_real_and_bounded():
    import sys
    import time
    if not sys.platform.startswith("linux"):
        pytest.skip("linux /proc/stat only")
    t0 = time.monotonic()
    out = cm.sample()
    assert time.monotonic() - t0 <= 2.0
    assert out is not None and 0 <= out["cpu_busy_pct"] <= 100 and 1000 <= out["cpu_busy_window_ms"] <= 2000


@pytest.mark.parametrize("pct,ok", [(49.999, True), (0.0, True), (50.0, False), (50.001, False), (100.0, False),
                                    (math.nan, False), (math.inf, False), (-0.1, False), (100.1, False),
                                    (None, False), ("30", False), (True, False)])
def test_below_is_strict_and_never_rounds(pct, ok):
    assert cm.below(pct, 50) is ok


@pytest.mark.parametrize("limit,ok", [(50, True), (100, True), (0.5, True), (0, False), (-1, False), (100.5, False),
                                      (math.nan, False), (math.inf, False), (None, False), ("50", False),
                                      (True, False)])
def test_valid_limit(limit, ok):
    assert cm.valid_limit(limit) is ok
    if not ok:
        assert cm.below(1.0, limit) is False  # an invalid limit never admits


def test_low_load_high_busy_rejects_and_high_load_low_busy_passes():
    # Busy is what gates the hub; normalized load1 is never a substitute.
    assert cm.below(cm.busy_pct(cm.parse_proc_stat(stat(idle=0)), cm.parse_proc_stat(stat(user=80, idle=20))),
                    50) is False
    assert cm.below(cm.busy_pct(cm.parse_proc_stat(stat(idle=0)), cm.parse_proc_stat(stat(user=10, idle=90))),
                    50) is True


def _reader(doc):
    def read(path):
        if isinstance(doc, Exception):
            raise doc
        return doc
    return read


@pytest.mark.parametrize("doc,expected", [
    (FileNotFoundError(), (cm.OFF, None)),
    (None, (cm.OFF, None)),
    ({}, (cm.OFF, None)),
    ({"kanban": {"admission": {}}}, (cm.OFF, None)),
    ({"kanban": {"admission": {"cpu_busy_below_pct": None}}}, (cm.OFF, None)),
    ({"kanban": {"admission": {"cpu_busy_below_pct": 50}}}, (cm.ON, 50.0)),
    ({"kanban": {"admission": {"cpu_busy_below_pct": 0}}}, (cm.UNKNOWN, None)),
    ({"kanban": {"admission": {"cpu_busy_below_pct": "50"}}}, (cm.UNKNOWN, None)),
    ({"kanban": {"admission": {"cpu_busy_below_pct": 150}}}, (cm.UNKNOWN, None)),
    ({"kanban": "nope"}, (cm.UNKNOWN, None)),
    (PermissionError("denied"), (cm.UNKNOWN, None)),
    (ValueError("bad yaml"), (cm.UNKNOWN, None)),
])
def test_root_limit_absent_disables_invalid_or_unreadable_refuses(tmp_path, doc, expected):
    assert cm.root_limit(read_doc=_reader(doc), root=tmp_path) == expected


def test_root_limit_reads_installation_root_not_profile(tmp_path, monkeypatch):
    root, profile = tmp_path / "root", tmp_path / "root" / "profiles" / "p"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: 50\n")
    (profile / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: 99\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    assert cm.root_limit() == (cm.ON, 50.0)
