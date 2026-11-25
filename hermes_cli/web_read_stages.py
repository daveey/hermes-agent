"""Fixed-cardinality passive timings for one coalesced read, not its arguments.

A follower joins the leader's read_id; worker stages retain the leader request
identity even after its caller cancels. A missing leader trace is NOT coverage.
No worker result, key, callable name, exception text or query is recorded.
"""
from __future__ import annotations

import uuid

from hermes_cli.web_request_stages import _request_trace


class ReadTrace:
    def __init__(self):
        self.state = _request_trace.get()
        self.read_id = uuid.uuid4().hex

    def emit(self, stage):
        if self.state is not None:
            sink, fields = self.state
            sink.emit(stage, read_id=self.read_id, **fields)

    def join(self):
        state = _request_trace.get()
        if state is not None:
            sink, fields = state
            sink.emit("read_join", read_id=self.read_id, **fields)

    async def run(self, runner, function):
        if self.state is None:
            return await runner(function)

        def observed():
            self.emit("read_worker_start")
            try:
                return function()
            finally:
                self.emit("read_worker_end")

        self.emit("read_runner_submit")
        try:
            return await runner(observed)
        finally:
            self.emit("read_result")
