"""Per-file peak RSS in scripts/run_tests_parallel.py (helpers: run_tests_parallel_rss.py).

The runner reaps each per-file pytest child with ``os.wait4``, prints the top
5 peak-RSS files beside "Top 10 slowest", and prints one
``⚠ high-memory test file`` line per file over HERMES_TEST_FILE_RSS_WARN_MB
(default 512). The warning never changes the exit code.

The end-to-end test below allocates ~600 MB in a grandchild. ``wait4`` folds
reaped descendants into the child's ``ru_maxrss`` on Linux and macOS, so this
file itself peaks near 600 MB and raises its own threshold:
"""
# run_tests_parallel: rss-warn-mb=1024

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
MB = 1024 * 1024


@pytest.fixture(scope="module")
def rtp():
    spec = importlib.util.spec_from_file_location("rtp_rss", _SCRIPTS / "run_tests_parallel_rss.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rtp_rss"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_maxrss_is_bytes_on_macos(rtp) -> None:
    assert rtp.maxrss_bytes(600 * MB, platform="darwin") == 600 * MB


@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_maxrss_is_kib_elsewhere(rtp, platform: str) -> None:
    assert rtp.maxrss_bytes(600 * 1024, platform=platform) == 600 * MB


def test_warn_threshold_env(rtp) -> None:
    assert rtp.rss_warn_mb({}) == 512
    assert rtp.rss_warn_mb({"HERMES_TEST_FILE_RSS_WARN_MB": "300"}) == 300
    # Garbage or non-positive values keep the default rather than silencing the check.
    assert rtp.rss_warn_mb({"HERMES_TEST_FILE_RSS_WARN_MB": "lots"}) == 512
    assert rtp.rss_warn_mb({"HERMES_TEST_FILE_RSS_WARN_MB": "0"}) == 512


def test_report_lists_top_five_and_warns_only_over_threshold(rtp, tmp_path: Path) -> None:
    files = {}
    for i, mb in enumerate([100, 700, 300, 513, 512, 50, 900]):
        f = tmp_path / f"test_f{i}.py"
        f.write_text("def test_x():\n    pass\n")
        files[f] = mb * MB
    lines = rtp.peak_rss_report(files, tmp_path, 512.0)

    assert lines[0] == "  Top 5 peak RSS:"
    top = lines[1:6]
    assert [ln.split()[0] for ln in top] == ["900", "700", "513", "512", "300"]
    warnings = [ln for ln in lines if "\u26a0 high-memory test file" in ln]
    # 512 MB is AT the threshold, not over it.
    assert [w.split(": ", 1)[1].split()[0] for w in warnings] == ["test_f6.py", "test_f1.py", "test_f3.py"]
    assert "peaked at 900 MB RSS (> 512 MB" in warnings[0]


def test_file_marker_raises_that_files_threshold(rtp, tmp_path: Path) -> None:
    heavy = tmp_path / "test_heavy.py"
    heavy.write_text("# run_tests_parallel: rss-warn-mb=2048\ndef test_x():\n    pass\n")
    plain = tmp_path / "test_plain.py"
    plain.write_text("def test_x():\n    pass\n")
    lines = rtp.peak_rss_report({heavy: 900 * MB, plain: 900 * MB}, tmp_path, 512.0)
    warnings = [ln for ln in lines if "high-memory" in ln]
    assert len(warnings) == 1 and "test_plain.py" in warnings[0]


def test_no_measurements_print_nothing(rtp, tmp_path: Path) -> None:
    assert rtp.peak_rss_report({}, tmp_path, 512.0) == []


@pytest.mark.skipif(not hasattr(os, "wait4"), reason="os.wait4 is POSIX-only")
def test_rusage_popen_records_the_childs_peak(rtp) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "b = b'x' * (64 * 1024 * 1024)"])
    rtp.reap_with_rusage(proc)
    proc.wait(timeout=60)
    assert proc.returncode == 0
    assert rtp.proc_peak_rss_bytes(proc) >= 64 * MB


def _probe_root(tmp_path: Path) -> Path:
    root = tmp_path / "runner-root"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(_SCRIPTS / "run_tests_parallel.py", root / "scripts")
    shutil.copy2(_SCRIPTS / "run_tests_parallel_rss.py", root / "scripts")
    shutil.copy2(_SCRIPTS / "run_tests_host_slots.py", root / "scripts")
    shutil.copy2(_SCRIPTS / "ci" / "list_os_marked_tests.py", root / "scripts" / "ci")
    return root


@pytest.mark.skipif(not hasattr(os, "wait4"), reason="os.wait4 is POSIX-only")
def test_runner_warns_about_a_600mb_file_and_keeps_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("HERMES_TEST_FILE_RSS_WARN_MB", raising=False)
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_hog.py").write_text(textwrap.dedent("""
        def test_allocates_600_mb():
            blob = b"x" * (600 * 1024 * 1024)  # filled, so every page is resident
            assert len(blob) == 600 * 1024 * 1024
    """))
    (probe / "test_light.py").write_text("def test_small():\n    assert True\n")
    runner = _probe_root(tmp_path) / "scripts" / "run_tests_parallel.py"

    proc = subprocess.run(
        [sys.executable, str(runner), "--paths", str(probe), "-j", "1", "--file-timeout", "120"],
        cwd=probe, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=180, check=False,
    )

    out = proc.stdout
    assert proc.returncode == 0, out
    assert "Top 10 slowest:" in out and "Top 2 peak RSS:" in out, out
    warnings = [ln for ln in out.splitlines() if "\u26a0 high-memory test file" in ln]
    assert len(warnings) == 1, out
    assert "test_hog.py" in warnings[0] and "(> 512 MB" in warnings[0], out
