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
        if self._policy is None:
            return {"ok": False, "error": "CLI launch has no parent dispatch policy"}
        previous = [(setter, getter()) for (getter, setter), _ in self._callbacks]
        try:
            for (_, setter), value in self._callbacks:
                setter(value)
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
            try:
                if reply.family != socket.AF_UNIX or reply.type != socket.SOCK_SEQPACKET:
                    return
                value = json.loads(data)
                if (not isinstance(value, dict) or value.get("nonce") != self._nonce
                        or not isinstance(value.get("args"), dict)):
                    return
                poller = select.poll()
                poller.register(reply, select.POLLHUP | select.POLLERR)
                if poller.poll(0):
                    return
                if self._requests >= MAX_REQUESTS:
                    raise ValueError("CLI launch request limit exceeded")
                self._requests += 1
                gate = self._gate.dispatch()
                result = self._ctx.copy().run(self._invoke, value["args"], gate)
            except Exception:  # health: allow BLE001 -- tool dispatch boundary; preserve gate outcome without leaking tool exceptions
                result = {"ok": False, "error": "CLI bridge dispatch refused"}
            finally:
                if gate is not None:
                    report = gate.finish(result)
                    if report["outcome"] == "outcome_unknown":
                        result = {**_unknown(), "task_id": report["task_id"]}
            if result is not None:
                reply.send(_encode(result))

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


def request(locator, args, *, timeout=30):
    """One attempt only. Offered-but-broken channels MUST NOT fall back to create."""
    try:
        value = json.loads(locator)
        fd = value["fd"]
        if type(fd) is not int or fd < 0 or not isinstance(value["nonce"], str):
            return _unknown()
        data = _encode({"nonce": value["nonce"], "args": args})
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
                response, _, flags, _ = reader.recvmsg(LIMIT)
                if flags & socket.MSG_TRUNC:
                    return _unknown()
                result = json.loads(response)
                return result if isinstance(result, dict) else _unknown()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _unknown()
