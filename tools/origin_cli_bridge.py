"""Launch-private native CLI transport. Locators are not origin authority.

Only the parent executes tools, under its captured live turn and strict receipt
transaction. A private reply socket per packet prevents sibling clients stealing
responses. Retirement fences admission without waiting for slow tool execution.
"""
from array import array
from contextlib import contextmanager
from contextvars import copy_context
import json
import os
import secrets
import select
import socket
import time
from threading import Lock, Thread

from hermes_cli.kanban_origin_admission import strict_local_creation
from hermes_cli.kanban_origin_cli_gate import LaunchGate

LIMIT = 65536
MAX_REQUESTS = 64
ENV_KEY = "HERMES_ORIGIN_CLI_BRIDGE"


def _encode(value):
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(data) > LIMIT:
        raise ValueError("CLI bridge packet too large")
    return data


def _unknown():
    return {"ok": False, "error": "CLI bridge outcome unknown; do not retry automatically",
            "outcome": "outcome_unknown"}


class Launch:
    """Capture on the caller thread; use spawn_endpoint ownership for Popen.

    This is not a sandbox: a shell can unset the locator and use legacy local
    creation, but that path receives no trusted origin receipt. Environment
    strings select a channel; they never establish origin custody.
    """
    def __init__(self, task_id):
        from tools.thread_context import _callback_api
        self._ctx = copy_context()
        self._callbacks = [(pair, pair[0]()) for pair in _callback_api()]
        from tools.origin_cli_policy import current_policy
        self._policy = current_policy()  # task_id is correlation, never a grant
        self._gate = LaunchGate()
        self._nonce = secrets.token_hex(24)
        self._server, self._child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._server.settimeout(0.1)
        self._closed = False
        self._spawn_lock = Lock()
        self._requests = 0
        self._displays = {}
        self._thread = Thread(target=self._serve, name="origin-cli-bridge", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._server.close()
            self._child.close()
            raise

    @property
    def child_fd(self):
        return self._child.fileno()

    @property
    def locator(self):
        return json.dumps({"fd": self.child_fd, "nonce": self._nonce})

    @contextmanager
    def spawn_endpoint(self):
        """Own a duplicate until Popen returns, never a borrowed fd number.

        Retirement can finish while Popen stalls. Already acquired endpoints
        stay valid, but the retired admission gate refuses their requests.
        """
        with self._spawn_lock:
            if self._closed:
                raise RuntimeError("CLI launch retired before spawn")
            endpoint = self._child.dup()
        with endpoint:
            fd = endpoint.fileno()
            yield fd, json.dumps({"fd": fd, "nonce": self._nonce})

    def retire(self):
        with self._spawn_lock:
            reports = self._gate.retire()
            self._closed = True
            self._displays.clear()
            self._child.close()
        try:
            self._server.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        return reports

    def close(self):
        self.retire()
        # Server owns its fd until its loop exits; never close underneath recvmsg.

    def _invoke(self, args, gate):
        return self._with_callbacks(args, gate, operation="create")

    def _invoke_release(self, args, gate):
        return self._with_callbacks(args, gate, operation="release")

    def _with_callbacks(self, args, gate, *, operation):
        if self._policy is None:
            return {"ok": False, "error": "CLI launch has no parent dispatch policy"}
        previous = [(setter, getter()) for (getter, setter), _ in self._callbacks]
        try:
            for (_, setter), value in self._callbacks:
                setter(value)
            if operation == "release":
                return self._policy.release(args, gate)
            if operation == "release_display":
                return self._policy.release_display(args)
            with strict_local_creation(gate=gate):
                return self._policy.create(args)
        finally:
            for setter, value in previous:
                setter(value)

    def _handle(self, data, flags, fds):
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(fds) != 1:
            return
        reply = socket.socket(fileno=fds[0])
        fds.clear()  # ownership transfers only after successful socket construction
        with reply:
            reply.settimeout(0.2)
            gate = None
            result = None
            operation = None
            try:
                if reply.family != socket.AF_UNIX or reply.type != socket.SOCK_SEQPACKET:
                    return
                value = json.loads(data)
                if (not isinstance(value, dict) or value.get("nonce") != self._nonce
                        or not isinstance(value.get("args"), dict)
                        or set(value) not in ({"nonce", "args"}, {"nonce", "args", "operation"})):
                    return
                operation = value.get("operation", "create")
                if operation not in ("create", "release", "release_display"):
                    return
                poller = select.poll()
                poller.register(reply, select.POLLHUP | select.POLLERR)
                if poller.poll(0):
                    return
                if self._requests >= MAX_REQUESTS:
                    raise ValueError("CLI launch request limit exceeded")
                self._requests += 1
                gate = self._gate.dispatch(operation="release" if operation == "release_display" else operation)
                if operation == "release_display":
                    args = value["args"]
                    if set(args) != {"receipt"} or not isinstance(args["receipt"], str):
                        raise ValueError("display accepts only its parent receipt")
                    with self._spawn_lock:
                        saved = self._displays.pop(args["receipt"], None)
                    if saved is None or time.monotonic() - saved[0] > 120:
                        raise ValueError("display receipt unavailable")
                    result = self._ctx.copy().run(self._with_callbacks, saved[1], gate, operation="release_display")
                else:
                    invoke = self._invoke if operation == "create" else self._invoke_release
                    result = self._ctx.copy().run(invoke, value["args"], gate)
            except Exception:  # health: allow BLE001 -- tool dispatch boundary; preserve gate outcome without leaking tool exceptions
                result = {"ok": False, "error": "CLI bridge dispatch refused"}
            finally:
                if gate is not None:
                    report = gate.finish(result)
                    if report["outcome"] == "outcome_unknown":
                        result = {**_unknown(), "task_id": report["task_id"]}
            if result is not None:
                if operation == "release":
                    from hermes_cli.kanban_release_read import DisplayResult
                    if type(result) is DisplayResult:
                        receipt = secrets.token_hex(32)
                        with self._spawn_lock:
                            if not self._closed:
                                self._displays[receipt] = (time.monotonic(), result.confirmation)
                        result = {**result, "display_receipt": receipt}
                from tools.origin_cli_reply import send
                send(reply, result, release=operation == "release")

    def _serve(self):
        try:
            while not self._closed:
                fds = []
                try:
                    data, ancillary, flags, _ = self._server.recvmsg(
                        LIMIT, socket.CMSG_SPACE(4 * 16), socket.MSG_CMSG_CLOEXEC)
                    for level, kind, payload in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            values = array("i")
                            values.frombytes(payload[:len(payload) - len(payload) % values.itemsize])
                            fds.extend(values)
                    if not data:
                        continue  # empty record is not EOF while parent holds child endpoint
                    self._handle(data, flags, fds)
                except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
                    continue
                finally:
                    for fd in fds:
                        os.close(fd)
        finally:
            self._server.close()


def request(locator, args, *, timeout=30, operation="create"):
    """One attempt only. Offered-but-broken channels MUST NOT fall back to create."""
    try:
        value = json.loads(locator)
        fd = value["fd"]
        if type(fd) is not int or fd < 0 or not isinstance(value["nonce"], str):
            return _unknown()
        if operation not in ("create", "release", "release_display"):
            return _unknown()
        packet = {"nonce": value["nonce"], "args": args}
        if operation != "create":  # Preserve the existing create wire bytes.
            packet["operation"] = operation
        data = _encode(packet)
        # Duplicate rather than steal the inherited launch socket from sibling calls.
        duplicate = os.dup(fd)
        try:
            channel = socket.socket(fileno=duplicate)
        except OSError:
            os.close(duplicate)
            raise
        with channel:
            if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_SEQPACKET:
                return _unknown()
            reader, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            with reader, writer:
                reader.settimeout(timeout)
                channel.settimeout(timeout)
                channel.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", [writer.fileno()]))])
                writer.close()  # only the server may keep the reply endpoint alive
                from tools.origin_cli_reply import receive
                return receive(reader, release=operation == "release", timeout=timeout)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _unknown()
