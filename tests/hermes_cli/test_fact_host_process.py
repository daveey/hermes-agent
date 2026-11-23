"""Actual process/IPC proof; no FakeStore or consumer-compatibility claim."""
import hashlib
import multiprocessing

import pytest

from hermes_cli.fact_ipc import connect, send, receive
from tests.hermes_cli.fact_host_processes import owner_main, replica_main, TOKENS, SCOPES
from tests.waiting import wait_until


def start(ctx, target, *args):
    parent, child = ctx.Pipe()
    process = ctx.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    assert parent.poll(15), "child startup timed out"
    return process, parent, parent.recv()


def rpc(pipe, command):
    pipe.send(command)
    assert pipe.poll(15), f"child command timed out: {str(command)[:50]}"
    return pipe.recv()


def stop(process, pipe):
    if process.is_alive():
        pipe.send("stop" if process.name.startswith("replica") else ("stop",))
        process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)
    pipe.close()


@pytest.mark.platforms("linux")
def test_owner_two_scoped_replicas_failover_large_reset_and_memory(tmp_path_factory):
    ctx = multiprocessing.get_context("spawn")
    directory = tmp_path_factory.mktemp("fh")
    owner, op, ready = start(ctx, owner_main, directory)
    assert ready == {"ready": True}
    serve, sp, _ = start(ctx, replica_main, directory, 0)
    gateway, gp, _ = start(ctx, replica_main, directory, 1)
    serve.name, gateway.name = "replica-serve", "replica-gateway"
    try:
        contender, cp, rejected = start(ctx, owner_main, directory)
        contender.join(5)
        cp.close()
        assert rejected == {"error": "BlockingIOError"}
        assert contender.exitcode == 0
        alpha, beta = b'{"panel":"alpha"}', b'{"panel":"beta-private"}'
        rpc(op, ("put", 0, alpha))
        rpc(op, ("put", 1, beta))
        assert wait_until(lambda: rpc(sp, "view").get("digest") == hashlib.sha256(alpha).hexdigest(), timeout=15)
        assert wait_until(lambda: rpc(gp, "view").get("digest") == hashlib.sha256(beta).hexdigest(), timeout=15)
        assert rpc(sp, "privacy") == {"denied": True, "scopes": {"alpha"}}
        assert rpc(gp, "privacy") == {"denied": True, "scopes": {"beta"}}
        old_epoch = rpc(sp, "view")["epoch"]
        # A single fact larger than a subscriber's entire byte budget recovers via pages.
        large = b"x" * (5 * 1024 * 1024)
        rpc(op, ("put", 0, large))
        digest = hashlib.sha256(large).hexdigest()
        assert wait_until(lambda: rpc(sp, "view").get("digest") == digest, timeout=20)
        assert rpc(gp, "view")["digest"] == hashlib.sha256(beta).hexdigest()
        stats = [rpc(op, ("stats",)), rpc(sp, "stats"), rpc(gp, "stats")]
        peak_kib = sum(s["rss_kib"] for s in stats)
        print({"owner_serve_gateway_peak_rss_kib": peak_kib, "processes": stats})
        assert peak_kib < 512 * 1024
        assert stats[1]["reads"] == stats[2]["reads"] == 0
        owner.terminate()  # only this fixture-owned process; kernel releases ownership
        owner.join(5)
        op.close()
        assert wait_until(lambda: rpc(sp, "view").get("state") == "Unavailable", timeout=10)
        owner, op, ready = start(ctx, owner_main, directory)
        assert ready == {"ready": True}
        rpc(op, ("put", 0, b"new-term"))
        assert wait_until(lambda: rpc(sp, "view").get("digest") == hashlib.sha256(b"new-term").hexdigest(), timeout=15)
        assert rpc(sp, "view")["epoch"] != old_epoch
    finally:
        stop(serve, sp)
        stop(gateway, gp)
        stop(owner, op)
    assert serve.exitcode == gateway.exitcode == owner.exitcode == 0


@pytest.mark.platforms("linux")
def test_real_slow_subscriber_reset_and_page_expiry(tmp_path_factory):
    ctx = multiprocessing.get_context("spawn")
    directory = tmp_path_factory.mktemp("fh")
    owner, op, _ = start(ctx, owner_main, directory)
    try:
        with connect(directory / "facts.sock") as sock:
            send(sock, {"version": 1, "token": TOKENS[0], "after": None})
            assert receive(sock)["scope"] == SCOPES[0]
            # Drain initial reset first, so this proves overflow of an established
            # subscriber rather than merely observing its initial reset_pending.
            send(sock, {"op": "next"})
            initial = receive(sock)["pin"]
            send(sock, {"op": "release", "token": initial["token"]})
            assert receive(sock) is True
            assert rpc(op, ("stats",))["queues"] == [(0, 0, False)]
            elapsed = rpc(op, ("burst",))["elapsed"]
            assert elapsed < 10
            stats = rpc(op, ("stats",))
            assert any(reset for _, _, reset in stats["queues"])
            assert all(size <= 4 * 1024 * 1024 and count <= 64 for size, count, _ in stats["queues"])
            send(sock, {"op": "next"})
            pin = receive(sock)["pin"]
            assert pin["rev"] == 100
            send(sock, {"op": "page", "token": pin["token"], "offset": 0})
            assert receive(sock)
            assert wait_until(lambda: rpc(op, ("stats",))["pins"] == 0, timeout=5)
            send(sock, {"op": "page", "token": pin["token"], "offset": 0})
            with pytest.raises(EOFError):
                receive(sock)
        with connect(directory / "facts.sock") as sock:
            send(sock, {"version": 1, "token": "forged"})
            with pytest.raises(EOFError):
                receive(sock)
    finally:
        stop(owner, op)
    assert owner.exitcode == 0
