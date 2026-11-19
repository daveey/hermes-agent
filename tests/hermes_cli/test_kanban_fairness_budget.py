"""Real on-disk aggregate bound across DB exports and runtime epochs."""
import threading

import pytest

from hermes_cli import kanban_fairness_budget as budget
from hermes_cli import kanban_fairness_segments as segments


def total(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def test_two_epoch_writes_share_bound(tmp_path):
    root = tmp_path / "capture"
    budget.initialize(root, 4096)
    one, two = root / "runtime" / "one", root / "runtime" / "two"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    segments.write(one / "segment-000001.jsonl", b"a" * 2500)
    with pytest.raises(budget.BudgetExceeded):
        segments.write(two / "segment-000001.jsonl", b"b" * 2500)
    assert total(root) <= 4096
    assert (one / "segment-000001.jsonl").read_bytes() == b"a" * 2500


def test_replacement_counts_temp_peak_and_orphans(tmp_path):
    root = tmp_path / "capture"
    budget.initialize(root, 4096)
    segments.write(root / "receipts.json", b"a" * 2000)
    (root / "orphan.tmp").write_bytes(b"x" * 500)
    with pytest.raises(budget.BudgetExceeded):
        segments.write(root / "receipts.json", b"b" * 2000)
    assert (root / "receipts.json").read_bytes() == b"a" * 2000
    assert (root / "orphan.tmp").exists()


def test_concurrent_writers_do_not_overshoot(tmp_path):
    root = tmp_path / "capture"
    budget.initialize(root, 4096)
    barrier = threading.Barrier(3)
    results = []

    def writer(name):
        barrier.wait()
        try:
            segments.write(root / name, b"a" * 2500)
            results.append(True)
        except budget.BudgetExceeded:
            results.append(False)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert sorted(results) == [False, True]
    assert total(root) <= 4096


def _process_writer(root, name, barrier, results):
    barrier.wait(timeout=10)
    try:
        segments.write(root / name, b"a" * 2500)
        results.put(True)
    except budget.BudgetExceeded:
        results.put(False)


def test_processes_share_root_lock(tmp_path):
    import multiprocessing

    root = tmp_path / "capture"
    budget.initialize(root, 4096)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(3), context.Queue()
    processes = [context.Process(target=_process_writer, args=(root, name, barrier, results))
                 for name in ("one", "two")]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert sorted(results.get(timeout=2) for _ in processes) == [False, True]
        assert total(root) <= 4096
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        results.close()
        results.join_thread()


def test_runtime_missing_budget_refuses(tmp_path):
    path = tmp_path / "capture" / "runtime" / "epoch"
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match="budget missing"):
        segments.write(path / "runtime-close.json", b"{}")
    assert not (path / "runtime-close.json").exists()


def test_existing_budget_cannot_be_silently_raised(tmp_path):
    budget.initialize(tmp_path, 4096)
    with pytest.raises(ValueError):
        budget.initialize(tmp_path, 8192)


def test_symlink_cannot_escape_budget(tmp_path):
    root = tmp_path / "capture"
    budget.initialize(root, 4096)
    (root / "outside").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        segments.write(root / "receipts.json", b"data")
    assert not (root / "receipts.json").exists()
