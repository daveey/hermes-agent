"""Real isolated owner/serve/gateway process targets for fact host proofs."""
import hashlib
import resource
import sqlite3
import time

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint

SCOPES = (Scope("test-install", "alpha"), Scope("test-install", "beta"))
TOKENS = ("a" * 48, "b" * 48)


class OwnerCommands:
    def __init__(self, host):
        self.host = host

    def put(self, index, data):
        key = self.host.store.view_key(SCOPES[index], "panel_status", {"session_id": "one"})
        return self.host.store.publish([Fact(key, {"encoded": data}, health="fresh")])

    def expire(self):
        key = self.host.store.view_key(SCOPES[0], "panel_status", {"session_id": "one"})
        return self.host.store.publish([Fact(key, {"encoded": b"expires"}, health="fresh",
                                             valid_until=time.time() + 2)])

    def dataset(self):
        for scope in SCOPES:
            for batch in range(24):
                self.host.store.publish(Fact(FactKey(scope, "TaskFact", str(batch * 500 + i)),
                                             {"title": "task", "status": "ready", "index": i})
                                        for i in range(500))
        return True

    def burst(self):
        start = time.monotonic()
        for i in range(100):
            self.put(0, str(i).encode())
        return {"elapsed": time.monotonic() - start}

    def stats(self):
        return {"rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "pins": self.host.pages.retained_bytes, "ring": self.host.store._retained_bytes,
                "queues": [(s.queued_bytes, len(s.queue), s.reset_pending)
                           for s in self.host.store._subscribers]}


def owner_main(directory, pipe):
    host = FactHost(directory, dict(zip(TOKENS, SCOPES)), page_ttl=3)
    try:
        host.start()
    except OSError as exc:
        pipe.send({"error": type(exc).__name__})
        return

    def demand(scope, view, selector):
        key = host.store.view_key(scope, view, selector)
        host.store.publish([Fact(key, {"encoded": b"demanded"}, health="fresh")])

    host.demand = demand
    commands = OwnerCommands(host)
    handlers = {"put": commands.put, "expire": commands.expire, "dataset": commands.dataset,
                "burst": commands.burst, "stats": commands.stats}
    pipe.send({"ready": True})
    try:
        while True:
            cmd = pipe.recv()
            if cmd[0] == "stop":
                break
            pipe.send(handlers[cmd[0]](*cmd[1:]))
    finally:
        host.close()
        pipe.close()


class ReplicaCommands:
    def __init__(self, handle, index):
        self.handle, self.index = handle, index
        self.reads = 0

    def no_db(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("replica attempted DB access")

    def view(self):
        snap = self.handle.v1.snapshot(SCOPES[self.index])
        result = snap.view("panel_status", {"session_id": "one"})
        if hasattr(result, "encoded"):
            return {"size": len(result.encoded), "digest": hashlib.sha256(result.encoded).hexdigest(),
                    "epoch": result.epoch, "rev": result.rev}
        return {"state": type(result).__name__}

    def demand(self):
        self.handle.demand("panel_status", {"session_id": "one"})
        return True

    def privacy(self):
        try:
            self.handle.v1.snapshot(SCOPES[1 - self.index])
        except ValueError:
            return {"denied": True, "scopes": {k.scope.profile_id for k in self.handle._store._facts}}
        return {"denied": False}

    def stats(self):
        return {"reads": self.reads, "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def replica_main(directory, index, pipe):
    commands = ReplicaCommands(None, index)
    sqlite3.connect = commands.no_db
    handle = acquire(SCOPES[index], endpoint=Endpoint(str(directory / "facts.sock"), TOKENS[index]))
    commands.handle = handle
    handlers = {"view": commands.view, "demand": commands.demand,
                "privacy": commands.privacy, "stats": commands.stats}
    pipe.send({"ready": True})
    try:
        while True:
            cmd = pipe.recv()
            if cmd == "stop":
                break
            pipe.send(handlers[cmd]())
    finally:
        handle.close()
        pipe.close()
