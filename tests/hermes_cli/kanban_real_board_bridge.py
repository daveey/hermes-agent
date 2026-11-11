"""Opt-in HTTP bridge to the actual hermes-hub Vitest Worker, never a fake Board."""
import json
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BoardBridge:
    def __init__(self):
        self.commands = queue.Queue()
        self.replies = queue.Queue()
        self.ready = queue.Queue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, value):
                body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                if self.path == "/next":
                    try:
                        value = owner.commands.get(timeout=1)
                    except queue.Empty:
                        value = {"noop": True}
                    self.respond(200, value)
                elif self.path in {"/ready", "/reply"}:
                    target = owner.ready if self.path == "/ready" else owner.replies
                    target.put(json.loads(body))
                    self.respond(200, {})
                else:
                    owner.commands.put({"path": self.path, "method": self.command,
                                        "headers": dict(self.headers), "body": body})
                    result = owner.replies.get(timeout=30)
                    self.respond(result["status"], result["text"])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def wait_ready(self, process, timeout=60):
        """Fail promptly on peer exit, while bounding a live-but-unready peer."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.ready.get_nowait()
            except queue.Empty:
                pass
            code = process.poll()
            if code is not None:
                raise RuntimeError(f"Worker bridge exited before readiness (exit {code})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Worker bridge not ready before deadline")
            try:
                return self.ready.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue

    def alarm(self, board, task, run):
        self.commands.put({"alarm": True, "board": board, "task": task, "run": run})
        return self.replies.get(timeout=30)

    def close(self, process):
        self.commands.put({"stop": True})
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
