"""Bounded startup demand channel independent of the long-poll stream. [daveey]"""
import queue
import threading

from hermes_cli.fact_ipc import connect, receive, send
from hermes_cli.fact_wire import encoded_size


class DemandChannel:
    def __init__(self, scope, endpoint):
        self.scope, self.endpoint = scope, endpoint
        self.queue = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.wake = threading.Event()
        # All access is authorized by explicit scope/token, never launch-profile env.
        self.thread = threading.Thread(target=self.run, name="fact-demand", daemon=True)
        self.thread.start()

    def put(self, view, selector):
        command = {"op": "demand", "view": view, "selector": dict(selector)}
        if encoded_size(command) > 64 * 1024:
            raise ValueError("demand_budget_exceeded")
        self.queue.put_nowait(command)
        self.wake.set()

    def run(self):
        while not self.stopped.is_set():
            self.wake.wait()
            self.wake.clear()
            while not self.stopped.is_set():
                try:
                    command = self.queue.get_nowait()
                except queue.Empty:
                    break
                self.deliver(command)

    def deliver(self, command):
        while not self.stopped.is_set():
            try:
                with connect(self.endpoint.path) as sock:
                    sock.settimeout(1)
                    send(sock, {"version": 1, "token": self.endpoint.token, "after": None})
                    if receive(sock)["scope"] != self.scope:
                        raise ValueError("unauthorized_scope")
                    send(sock, command)
                    receive(sock)
                    return
            except (OSError, EOFError, ValueError, TypeError, KeyError):
                if self.stopped.wait(.1):
                    return

    def close(self):
        self.stopped.set()
        self.wake.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("demand_shutdown_timeout")
