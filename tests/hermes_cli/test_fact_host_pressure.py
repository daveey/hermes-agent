"""Owner deadline and three-process measured workload, not live acceptance."""
import hashlib
import json
import multiprocessing

import pytest

from tests.hermes_cli.fact_host_processes import owner_main, replica_main
from tests.hermes_cli.test_fact_host_process import start, rpc, stop
from tests.waiting import wait_until


@pytest.mark.platforms("linux")
def test_owner_expiry_reaches_replica_without_reads(tmp_path_factory):
    ctx = multiprocessing.get_context("spawn")
    directory = tmp_path_factory.mktemp("expiry")
    owner, op, _ = start(ctx, owner_main, directory)
    serve, sp, _ = start(ctx, replica_main, directory, 0)
    serve.name = "replica-serve"
    try:
        rpc(op, ("expire",))
        assert wait_until(lambda: rpc(sp, "view").get("digest") == hashlib.sha256(b"expires").hexdigest(), timeout=10)
        assert wait_until(lambda: rpc(sp, "view").get("state") == "Unavailable", timeout=10)
        assert rpc(sp, "stats")["reads"] == 0
        # There is no remaining source activity; demand must interrupt neither
        # reader nor producer and must not wait the 20-second next timeout.
        rpc(sp, "demand")
        assert wait_until(lambda: rpc(sp, "view").get("digest") == hashlib.sha256(b"demanded").hexdigest(), timeout=5)
    finally:
        stop(serve, sp)
        stop(owner, op)
    assert serve.exitcode == owner.exitcode == 0


@pytest.mark.platforms("linux")
def test_three_process_dataset_large_resets_memory(tmp_path_factory, record_property):
    ctx = multiprocessing.get_context("spawn")
    directory = tmp_path_factory.mktemp("pressure")
    owner, op, _ = start(ctx, owner_main, directory)
    serve, sp, _ = start(ctx, replica_main, directory, 0)
    gateway, gp, _ = start(ctx, replica_main, directory, 1)
    serve.name, gateway.name = "replica-serve", "replica-gateway"
    try:
        rpc(op, ("dataset",))
        # Replace both profiles' large views twice; include retained old snapshots,
        # wire buffers, paged resets and replica replacement in peak measurements.
        for generation in range(2):
            for index, pipe in enumerate((sp, gp)):
                data = bytes([65 + generation + index]) * (12 * 1024 * 1024)
                rpc(op, ("put", index, data))
                digest = hashlib.sha256(data).hexdigest()
                assert wait_until(lambda: rpc(pipe, "view").get("digest") == digest, timeout=30)
        stats = [rpc(op, ("stats",)), rpc(sp, "stats"), rpc(gp, "stats")]
        peak_kib = sum(s["rss_kib"] for s in stats)
        record_property("memory_evidence", json.dumps({"peak_sum_kib": peak_kib, "processes": stats,
                                                       "facts": 24000, "view_bytes_each": 12 * 1024 * 1024}))
        assert peak_kib < 512 * 1024
        assert stats[0]["ring"] <= 16 * 1024 * 1024
        assert stats[1]["reads"] == stats[2]["reads"] == 0
        assert rpc(sp, "privacy")["scopes"] == {"alpha"}
        assert rpc(gp, "privacy")["scopes"] == {"beta"}
    finally:
        stop(serve, sp)
        stop(gateway, gp)
        stop(owner, op)
    assert serve.exitcode == gateway.exitcode == owner.exitcode == 0
