"""[daveey] Warm blue/green handoff for the always-on dashboard on Linux (systemd --user).

``hermes_cli/always_on_dashboard.py`` used to exec ONE ``hermes serve`` on the fixed port 9147 under
``hermes-always-on-dashboard.service``. Every core update (``hermes update``'s dashboard refresh),
mods-stale and core-stale pass restarts that unit, and systemd stops the old process before it starts
the new one. The port then refused connections for seconds, and the new process's first requests paid
every per-process cold cache: 7-30 s for session-cards, session-kanban-tasks, kanban/board and
profiles/sessions. That is past Desktop's 15 s and 30 s timeouts, so every hub core update showed up
as a burst of Desktop timeouts (hermes-hub 2026-11-12: 38 in 30 min, t_01a1d09123dbb9c4).

Three units now share the work. Every restart caller keeps naming ``hermes-always-on-dashboard``.

* ``hermes-always-on-proxy.service``: a stdlib TCP splice that owns 127.0.0.1:9147, never restarts on
  an update, and writes ``record.json`` with its own stable pid. Each new connection goes to the port
  ``active.json`` names. While that port refuses, the client is held and retried; it is never refused.
* ``hermes-always-on-backend@<port>.service`` (ports 9148-9151): the same isolated SSH ``hermes serve``
  as before, with ``HERMES_ALWAYS_ON_BACKEND=<port>``. Such a backend does not self-retire on code
  skew. It retires through the idle fence once ``active.json`` stops naming its port
  (``web_server_skew_exit.start_superseded_watchdog``), so live turns are never cut by a roll. Two
  backstops stop a backend that never goes idle (SIGTERM, its turns checkpoint): any superseded one after
  SUPERSEDED_HARD_S (6 h), and one rolled away from because it stopped answering after DEMOTED_HARD_S (30 min).
* ``hermes-always-on-dashboard.service`` (the roller, ``Type=simple``, so a restart returns at once):
  it starts a backend on a free port, waits for it to answer, warms it by replaying the reads Desktop
  actually made, then switches ``active.json``. Afterwards it watches the active backend: a dead
  backend, a commit behind the checkout HEAD, a replaced runtime, or a backend that is running but has not
  answered /api/health for UNRESPONSIVE_CONFIRMATIONS polls starts another roll.

``hermes update`` no longer kills a ``backend@`` pid. It asks the roller to roll instead
(``route_always_on_backends``, called from ``dashboard_procs._kill_stale_dashboard_processes``), because a cold restart of the active backend is exactly
what this replaces. Everything here is stdlib only: the hub runs this file with the system python3,
next to an interim copy of ``always_on_dashboard.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    from hermes_cli import always_on_dashboard as aod
except ImportError:  # standalone copy next to always_on_dashboard.py (system python3, no hermes_cli)
    import always_on_dashboard as aod  # type: ignore[no-redef]

PROXY_UNIT = "hermes-always-on-proxy.service"
BACKEND_TEMPLATE = "hermes-always-on-backend@.service"
ROLLER_UNIT = aod.SYSTEMD_UNIT
BACKEND_UNIT_RE = re.compile(r"hermes-always-on-backend@(\d+)\.service")
BACKEND_PORTS = (9148, 9149, 9150, 9151)
ENV_BACKEND = "HERMES_ALWAYS_ON_BACKEND"
ACTIVE_NAME = "active.json"
ACTIVE_SCHEMA = 1
# Control-plane GETs a busy superseded backend may fetch from the active successor (never writes/prompts).
CONTROL_READ_PATHS = frozenset({"/api/model/options"})
HANDOFF_TIMEOUT_S = 5.0
_THIS_PORT: Optional[int] = None

PROXY_HOLD_S = 30.0          # a client waits this long for an answering backend before its connection is closed
PROXY_CONNECT_TIMEOUT_S = 5.0
BOOT_TIMEOUT_S = 180.0       # a new backend that does not answer /api/health by then is given up (old one stays)
WARM_CAP_S = 75.0            # the warm rounds stop here; the backend answers, so it is switched in anyway
WARM_REQUEST_TIMEOUT_S = 60.0
WARM_WORKERS = 6
WARM_MAX_REPLAYS = 40
SUPERVISE_POLL_S = 15.0
DRIFT_CONFIRMATIONS = 2      # consecutive polls a commit/runtime drift must persist before it rolls
# Consecutive polls an alive backend must miss /api/health before it rolls (~1-2 min at SUPERVISE_POLL_S). Higher
# than DRIFT_CONFIRMATIONS: a busy backend stalls for seconds routinely, and a roll leaves the old one running.
UNRESPONSIVE_CONFIRMATIONS = 4
START_PROBE_GAP_S = 3.0      # between the start-up health probes of the active backend (Roller.start)
UNRESPONSIVE_REASON = "does not answer /api/health"
FAILED_ROLL_BACKOFF_S = 300.0
SUPERSEDED_GRACE_S = 30.0    # connection-drain cap (web_server_skew_exit.DRAIN_CAP_S); then try the idle fence
SUPERSEDED_HARD_S = 6 * 3600.0  # ... and stopped (SIGTERM, turns checkpoint) once this old, busy or not
SUPERSEDED_NAME = "superseded.json"
# Backends rolled away from because they stopped answering: never adopted again, and stopped after
# DEMOTED_HARD_S (SIGTERM checkpoints their turns) instead of SUPERSEDED_HARD_S, so a starved backend that
# never passes its idle fence cannot hold one of the four BACKEND_PORTS for hours.
DEMOTED_NAME = "demoted.json"
DEMOTED_HARD_S = 1800.0
LOOP_WATCH_TAIL_BYTES = 4 * 1024 * 1024

# Read-only GETs safe to replay into a warming backend. Anything else Desktop polls (a courier outbox
# that claims items, a POST) is never replayed.
REPLAY_PATHS = frozenset({
    "/api/plugins/kanban/board", "/api/plugins/kanban/boards", "/api/plugins/kanban/session-labels",
    "/api/plugins/user-steps/session-cards", "/api/plugins/user-steps/requests",
    "/api/session-kanban-tasks", "/api/profiles", "/api/profiles/sessions", "/api/profiles/sessions/stamps",
    "/api/profiles/projects/tree", "/api/sessions", "/api/status", "/api/plugins/hub-sync/status",
    "/api/plugins/runners/v1/fleet", "/api/plugins/throughput/v1/series", "/api/plugins/hermes-monitor/workers",
    "/api/plugins/inbox/items", "/api/cron/jobs", "/api/plugins/core-update/status",
    "/api/plugins/account-usage/last", "/api/plugins/account-usage/selection",
})
DEFAULT_WARM = (
    "/api/plugins/kanban/board?board=default",
    "/api/plugins/kanban/boards",
    "/api/profiles/sessions?limit=50&offset=0&order=recent&profile=all&min_messages=1&archived=exclude",
    "/api/sessions?limit=50&offset=0&min_messages=1",
    "/api/plugins/hub-sync/status",
    "/api/status",
)


def log(message: str) -> None:
    print(f"always-on: {message}", file=sys.stderr, flush=True)


# ── shared state: active.json ────────────────────────────────────────────────────────────────────────


def active_path(home: Optional[Path] = None) -> Path:
    return aod.state_dir(home) / ACTIVE_NAME


def read_active(home: Optional[Path] = None) -> Optional[dict]:
    """``{"port": int, "warming": [int], ...}`` or None when missing or malformed."""
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(active_path(home).read_text(encoding="utf-8-sig"))
        port, warming = data.get("port"), data.get("warming", [])
        if (data.get("schemaVersion") == ACTIVE_SCHEMA and isinstance(port, int) and port > 0
                and isinstance(warming, list) and all(isinstance(p, int) for p in warming)):
            return data
    return None


def write_active(port: int, *, warming: Iterable[int] = (), home: Optional[Path] = None, **extra) -> None:
    """Atomic: the proxy and every backend's superseded watchdog read it at any instant."""
    aod._private_dir(aod.state_dir(home))
    record = {"schemaVersion": ACTIVE_SCHEMA, "port": int(port), "warming": sorted({int(p) for p in warming}),
              "updatedAt": time.time(), **extra}
    aod._write_private(active_path(home), json.dumps(record))


def is_superseded(port: int, home: Optional[Path] = None) -> bool:
    """True when ``active.json`` names neither this port as active nor as warming. A missing or unreadable
    record proves nothing, so it never retires a backend."""
    record = read_active(home)
    return record is not None and port != record["port"] and port not in record["warming"]


def successor_pending(port: int, home: Optional[Path] = None) -> bool:
    """In backend *port*: True while a successor is on its way, i.e. ``active.json`` names another port as
    warming (a roll is booting or warming it) or has already switched away from this port. A turn paused for
    a core update keeps waiting for that successor (``tui_gateway.update_pause``) instead of continuing here
    on stale code. A missing record proves nothing."""
    record = read_active(home)
    if record is None or port in record["warming"]:
        return False  # no proof, or this backend is itself the one warming
    return port != record["port"] or bool(record["warming"])


def model_options_control_path(*, explicit_only: bool = False, include_unconfigured: bool = False,
                               refresh: bool = False, profile: Optional[str] = None) -> str:
    """GET path the busy predecessor forwards to the successor (lowercase bools FastAPI accepts)."""
    query: list[tuple[str, str]] = []
    if profile:
        query.append(("profile", str(profile)))
    if refresh:
        query.append(("refresh", "true"))
    if include_unconfigured:
        query.append(("include_unconfigured", "true"))
    if explicit_only:
        query.append(("explicit_only", "true"))
    path = "/api/model/options"
    return f"{path}?{urllib.parse.urlencode(query)}" if query else path


def handoff_control_read(port: int, path: str, *, get: Optional[Callable] = None,
                         home: Optional[Path] = None,
                         timeout_s: float = HANDOFF_TIMEOUT_S) -> Optional[dict]:
    """If *port* is superseded, the active successor's GET *path* body.

    None when this process still serves, *path* is not a control read, or the successor
    did not answer 200 JSON. Never runs the local handler (stale ``sys.modules``)."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if port not in BACKEND_PORTS:
        return None
    parsed = urllib.parse.urlparse(path)
    if parsed.path not in CONTROL_READ_PATHS:
        return None
    try:
        if not is_superseded(port, home):
            return None
        record = read_active(home)
        if record is None:
            return None
        successor = record["port"]
        if successor not in BACKEND_PORTS:
            return None
        getter = get or _loopback_control_get
        status, _elapsed, body = getter(successor, path, timeout_s)
    except Exception:  # health: allow BLE001 -- a dead successor is not a stale-module bypass
        return None
    return body if status == 200 and isinstance(body, dict) else None


def _loopback_control_get(port: int, path: str, timeout: float) -> tuple[Optional[int], float, Optional[dict]]:
    # Loopback-only always-on backend (serve --isolated, never gated). Path is a parameter
    # (CONTROL_READ_PATHS); same exemption class as _http_get for the session-token route scanner.
    token, _nonce = aod.ensure_credentials()
    started = time.monotonic()
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}{path}",
                                     headers={"X-Hermes-Session-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, time.monotonic() - started, None
    except (OSError, ValueError, http.client.HTTPException):
        return None, time.monotonic() - started, None
    parsed = None
    with contextlib.suppress(ValueError):
        parsed = json.loads(body) if body[:1] in (b"{", b"[") else None
    return status, time.monotonic() - started, parsed if isinstance(parsed, dict) else None


def backend_port_for_pid(pid: int, proc: Path = Path("/proc")) -> Optional[int]:
    """The port of the ``hermes-always-on-backend@<port>.service`` unit whose cgroup holds *pid*, else None.
    By cgroup, not environment: an agent child of a backend inherits its environment, not its unit."""
    with contextlib.suppress(OSError, ValueError):
        match = BACKEND_UNIT_RE.search((proc / str(int(pid)) / "cgroup").read_text(encoding="utf-8-sig"))
        if match:
            return int(match.group(1))
    return None


def pop_backend_port(environ=os.environ) -> Optional[int]:
    """In a backend: the port its unit passed in ``HERMES_ALWAYS_ON_BACKEND``, removed from the environment
    so agent children never inherit it. None everywhere else."""
    global _THIS_PORT
    raw = environ.pop(ENV_BACKEND, "")
    port = int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else None
    if port is not None:
        _THIS_PORT = port
    return port


def this_backend_port() -> Optional[int]:
    """The always-on backend port this process was started as, else None."""
    return _THIS_PORT


def request_roll(run: Callable = subprocess.run) -> bool:
    """Ask the roller unit for a warm roll without waiting for it (``hermes update``'s dashboard refresh)."""
    try:
        from hermes_cli.always_on_deployment import refresh_standalone_controller
        refresh_standalone_controller()
        return run(["systemctl", "--user", "restart", "--no-block", ROLLER_UNIT], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"warm roll request failed: {exc}")
        return False


def route_always_on_backends(pids: Iterable[int], *, roll: Callable[[], bool] = request_roll,
                             port_for_pid: Callable[[int], Optional[int]] = backend_port_for_pid) -> list[int]:
    """``hermes update``'s dashboard refresh: the pids it may still kill-and-restart. Always-on backend
    pids are dropped, and the roller is asked for ONE warm roll instead, because a cold restart of the
    active backend is what this module replaces. A superseded backend retires by itself when idle."""
    pids = list(pids)
    routed = [pid for pid in pids if port_for_pid(pid) is not None]
    if routed:
        ok = roll()
        print(f"    {'✓' if ok else '⚠'} always-on dashboard: "
              f"{'requested a warm roll' if ok else 'warm roll request failed'} instead of restarting"
              f" PID {', '.join(map(str, routed))}")
    return [pid for pid in pids if pid not in routed]


def route_update_pids(pids: list[int], *, restart_managed: bool) -> list[int]:
    """``_kill_stale_dashboard_processes``'s hook: only an update refresh on Linux (where the units exist)
    routes always-on backends to a warm roll; ``dashboard --stop`` and other platforms keep every pid."""
    if not (pids and restart_managed and _on_linux()):
        return pids
    return route_always_on_backends(pids)


def _on_linux() -> bool:
    return sys.platform.startswith("linux")


# ── the proxy ────────────────────────────────────────────────────────────────────────────────────────


class Proxy:
    """Splices each accepted connection to the active backend. The target is read per connection, so a
    switch moves new connections only; existing ones stay on the backend that accepted them."""

    def __init__(self, *, home: Optional[Path] = None, hold_s: float = PROXY_HOLD_S,
                 clock: Callable[[], float] = time.monotonic):
        self.home, self.hold_s, self.clock = home, hold_s, clock
        self._cached: tuple[Optional[tuple[int, int]], Optional[int]] = (None, None)  # ((ino, mtime_ns), port)
        self.last_port: Optional[int] = None

    def target(self) -> Optional[int]:
        try:
            st = active_path(self.home).stat()
        except OSError:
            return None
        key = (st.st_ino, st.st_mtime_ns)  # each atomic replace is a new inode, even inside one mtime tick
        if self._cached[0] != key:
            record = read_active(self.home)
            self._cached = (key, record["port"] if record else None)
            if record and record["port"] != self.last_port:
                log(f"proxy: routing new connections to :{record['port']}")
                self.last_port = record["port"]
        return self._cached[1]

    async def _upstream(self):
        deadline = self.clock() + self.hold_s
        while True:
            port = self.target()
            if port:
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    return await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port),
                                                  PROXY_CONNECT_TIMEOUT_S)
            if self.clock() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def handle(self, client_reader, client_writer) -> None:
        upstream = await self._upstream()
        if upstream is None:
            log(f"proxy: no backend answered within {self.hold_s:.0f}s; closing a client connection")
            client_writer.close()
            return
        up_reader, up_writer = upstream
        # Each direction half-closes its destination on EOF; both sockets close only once both directions
        # are done, so a client that shuts down its write side after the request still gets the response.
        # A direction that breaks (a reset) ends the other: nothing more can flow through this splice.
        tasks = {asyncio.ensure_future(_pipe(client_reader, up_writer)),
                 asyncio.ensure_future(_pipe(up_reader, client_writer))}
        try:
            while tasks:
                done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if any(task.result() is False for task in done):
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for writer in (up_writer, client_writer):
                with contextlib.suppress(OSError, RuntimeError):
                    writer.close()


async def _pipe(reader, writer) -> bool:
    """Copy until EOF, then half-close *writer*. True on a clean EOF, False when either side broke. It never
    closes *writer*: ``Proxy.handle`` closes both sockets once both directions are done."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()
        return True
    except (ConnectionError, OSError):
        return False


def bind_listener(port: int, *, wait_s: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> socket.socket:
    """127.0.0.1:<port>, retried while a previous owner (the pre-proxy serve on first install) lets go."""
    deadline = time.monotonic() + wait_s
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", int(port)))
            sock.listen(2048)
            sock.setblocking(False)
            return sock
        except OSError:
            sock.close()
            if time.monotonic() >= deadline:
                raise
            sleep(0.5)


def cmd_proxy(args) -> int:
    hermes = os.path.abspath(args.hermes)
    sock = bind_listener(args.port)
    _token, nonce = aod.ensure_credentials()
    aod.write_record(pid=os.getpid(), port=args.port, nonce=nonce, hermes_home=str(aod._root_hermes_home()),
                     hermes_path=hermes)
    log(f"proxy: listening on 127.0.0.1:{args.port} (pid {os.getpid()})")
    proxy = Proxy()

    async def main() -> None:
        server = await asyncio.start_server(proxy.handle, sock=sock)
        async with server:
            await server.serve_forever()

    asyncio.run(main())
    return 0


# ── a backend unit ───────────────────────────────────────────────────────────────────────────────────

CUSTODY_DIR = "backends"
CUSTODY_SCHEMA = 1


def _proc_starttime(pid: int, proc: Path = Path("/proc")) -> Optional[str]:
    """``linux:<field 22 of /proc/<pid>/stat>`` (start time in clock ticks since boot), else None. exec keeps
    the pid and this value, so a record written just before ``execve`` names the serve that follows."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        stat = (proc / str(int(pid)) / "stat").read_text(encoding="utf-8-sig", errors="replace")
        return f"linux:{int(stat.rsplit(')', 1)[1].split()[19])}"
    return None


def custody_path(port: int, home: Optional[Path] = None) -> Path:
    return aod.state_dir(home) / CUSTODY_DIR / f"{int(port)}.json"


def write_backend_custody(*, port: int, nonce: str, hermes: str, home: Optional[Path] = None,
                          pid: Optional[int] = None) -> dict:
    """[daveey] Per-backend custody record ``desktop-ssh/always-on/backends/<port>.json`` (0600), written by
    ``cmd_backend`` immediately before it execs the serve. A systemd-owned always-on backend has no Desktop
    ``backend.lock.json`` (only a Desktop owned spawn writes one, and several backends share the always-on
    slot during a roll, so one slot lock cannot name them all). This record is that backend's proof of
    custody instead; ``verify_backend_custody`` checks it against the live process."""
    pid = os.getpid() if pid is None else int(pid)
    record = {"schemaVersion": CUSTODY_SCHEMA, "kind": "always-on-backend", "pid": pid,
              "creationTime": _proc_starttime(pid) or "", "port": int(port), "unit": backend_unit(port),
              "ownershipId": aod.OWNERSHIP_ID, "spawnNonce": nonce, "hermesHome": str(aod._root_hermes_home()),
              "hermesPath": hermes, "writtenAt": time.time()}
    path = custody_path(port, home)
    aod._private_dir(path.parent)
    aod._write_private(path, json.dumps(record))
    return record


def verify_backend_custody(pid: int, *, home: Optional[Path] = None, proc: Path = Path("/proc"),
                           nonce: Optional[str] = None) -> Optional[dict]:
    """The custody record of always-on backend *pid* when every fact agrees, else None: the pid runs in the
    cgroup of ``hermes-always-on-backend@<port>.service``; ``backends/<port>.json`` names this pid and its
    current kernel start time (so a recycled pid never matches); the record's nonce is the persisted
    always-on nonce (``always-on/nonce``) and appears in the process argv as ``--ssh-owner-nonce``."""
    port = backend_port_for_pid(pid, proc)
    if port is None:
        return None
    try:
        record = json.loads(custody_path(port, home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schemaVersion") != CUSTODY_SCHEMA:
        return None
    started = _proc_starttime(pid, proc)
    if record.get("pid") != int(pid) or record.get("port") != port or not started \
            or record.get("creationTime") != started or record.get("ownershipId") != aod.OWNERSHIP_ID:
        return None
    if nonce is None:
        with contextlib.suppress(OSError):
            nonce = (aod.state_dir(home) / "nonce").read_text(encoding="utf-8-sig").strip()
    if not nonce or record.get("spawnNonce") != nonce:
        return None
    try:
        argv = (proc / str(int(pid)) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    if b"--ssh-owner-nonce" not in argv or argv.index(b"--ssh-owner-nonce") + 1 >= len(argv) \
            or argv[argv.index(b"--ssh-owner-nonce") + 1] != nonce.encode():
        return None
    return record


def cmd_backend(args) -> int:
    """``hermes-always-on-backend@<port>``: stage the persisted token, record custody, exec the isolated SSH serve."""
    hermes = os.path.abspath(args.hermes)
    token, nonce = aod.ensure_credentials()
    token_path = aod.stage_token_file(token, nonce)
    env = dict(os.environ)
    env["HERMES_DESKTOP"] = "1"
    env[aod.ENV_FLAG] = "1"
    env[ENV_BACKEND] = str(int(args.port))
    env.pop("HERMES_DASHBOARD_SESSION_TOKEN", None)
    argv = aod.build_serve_argv(hermes, args.port, token_path, nonce)
    write_backend_custody(port=args.port, nonce=nonce, hermes=hermes)
    log(f"backend :{args.port}: exec {' '.join(argv[:7])} ...")
    os.execve(hermes, argv, env)
    return 0  # unreachable


# ── the roller ───────────────────────────────────────────────────────────────────────────────────────


def _systemctl(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          check=False)


def backend_unit(port: int) -> str:
    return f"hermes-always-on-backend@{int(port)}.service"


class Roller:
    """One roll: pick a port, start (or adopt) a backend there, warm it, switch ``active.json``. The previous
    backend is never stopped here: its superseded watchdog retires it once it is idle."""

    def __init__(self, *, token: str, checkout: Optional[Path], marker: Path, home: Optional[Path] = None,
                 systemctl: Callable[..., subprocess.CompletedProcess] = _systemctl,
                 get: Optional[Callable] = None, post: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, loop_watch: Optional[Path] = None,
                 ports: tuple[int, ...] = BACKEND_PORTS, boot_timeout_s: float = BOOT_TIMEOUT_S,
                 warm_cap_s: float = WARM_CAP_S):
        self.token, self.checkout, self.marker, self.home = token, checkout, marker, home
        self.systemctl, self.clock, self.sleep, self.ports = systemctl, clock, sleep, ports
        self.get = get or self._http_get
        self.post = post or self._http_post
        self.wall = wall
        self.loop_watch = loop_watch
        self.boot_timeout_s, self.warm_cap_s = boot_timeout_s, warm_cap_s

    # -- probes --
    def _http_get(self, port: int, path: str, timeout: float) -> tuple[Optional[int], float, Optional[dict]]:
        started = time.monotonic()
        request = urllib.request.Request(f"http://127.0.0.1:{int(port)}{path}",
                                         headers={"X-Hermes-Session-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            return exc.code, time.monotonic() - started, None
        except (OSError, ValueError, http.client.HTTPException):
            return None, time.monotonic() - started, None
        parsed = None
        with contextlib.suppress(ValueError):
            parsed = json.loads(body) if body[:1] in (b"{", b"[") else None
        return status, time.monotonic() - started, parsed if isinstance(parsed, dict) else None

    def _http_post(self, port: int, path: str, payload: dict) -> Optional[dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{int(port)}{path}", method="POST",
                                         data=json.dumps(payload).encode(),
                                         headers={"X-Hermes-Session-Token": self.token,
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                parsed = json.loads(response.read(65536))
        except (OSError, ValueError, http.client.HTTPException):
            return None
        return parsed if isinstance(parsed, dict) else None

    def unit_active(self, port: int) -> bool:
        try:
            state = (self.systemctl("is-active", backend_unit(port), timeout=15).stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return False
        return state in ("active", "activating", "reloading")

    def unit_pid(self, port: int) -> int:
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
            out = self.systemctl("show", "-p", "MainPID", "--value", backend_unit(port), timeout=15).stdout
            return int((out or "0").strip() or 0)
        return 0

    def health(self, port: int) -> Optional[dict]:
        status, _elapsed, body = self.get(port, "/api/health", 10.0)
        return body if status == 200 and body and body.get("ok") else None

    def runtime_intact(self, port: int) -> Optional[bool]:
        status, _elapsed, body = self.get(port, "/api/ssh/ownership", 10.0)
        if status != 200 or not body:
            return None
        return bool(body.get("runtimeIntact", True))

    def head(self) -> Optional[str]:
        if self.checkout is None:
            return None
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            r = subprocess.run(["git", "-C", str(self.checkout), "rev-parse", "HEAD"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
            sha = (r.stdout or "").strip()
            return sha if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", sha) else None
        return None

    @staticmethod
    def same_commit(commit: object, head: Optional[str]) -> Optional[bool]:
        """None when either side is unknown (never a reason to roll)."""
        if not head or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
            return None
        return head.startswith(commit) or commit.startswith(head)

    def update_in_progress(self) -> bool:
        return self.marker.exists()

    # -- the roll --
    def pick_port(self, active_port: Optional[int], head: Optional[str],
                  warming: Iterable[int] = ()) -> tuple[Optional[int], bool]:
        """(port, adopted). Adopt a non-active backend a restart cut short: one already answering on HEAD,
        or one the previous roll recorded as warming that is still booting, so restarts close together never
        stack cold boots onto the pool. Else the first port with no unit; else None."""
        warming = set(warming)
        demoted = self._load_demoted()
        others = [p for p in self.ports if p != active_port]
        running = [p for p in others if self.unit_active(p)]
        for port in running:
            if port in demoted:
                continue  # rolled away from for not answering: it holds its port until reaped, never serves again
            body = self.health(port)
            if body and self.same_commit(body.get("commit"), head):
                return port, True
            if body is None and port in warming:
                return port, True  # still booting for the roll this restart cut short
        for port in others:
            if port not in running and not _port_listening(port):
                return port, False
        return None, False

    def admission_deferred(self, reason: str) -> bool:
        """Keep launch outside the update hold; supervision retries with its normal backoff.

        marker lives in the installation root (not the active backend's profile home).
        Stay stdlib-only like the standalone roller; match admission_fence.is_held's lstat.
        The serve-side fence remains authoritative for a hold arriving after this check.
        """
        hold = self.marker.parent / "core-update" / "holds" / "hermes-sync"
        try:
            hold.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            log(f"roll ({reason}) deferred: cannot read update admission hold {hold}: {exc}; keeping current backend")
            return True
        log(f"roll ({reason}) deferred: update admission hold {hold} is set; keeping current backend")
        return True

    def roll(self, reason: str, *, marker_waited: bool = False) -> bool:
        """*marker_waited*: the caller already waited out the update marker (Roller.start), so a stale marker
        does not hold the roll for a second UPDATE_WAIT_S."""
        if self.admission_deferred(reason):
            return False
        if not marker_waited and not aod.wait_for_update_clear(self.marker, sleep=self.sleep, now=self.clock):
            log("update marker still present; rolling anyway")
        record = read_active(self.home)
        active_port = record["port"] if record and self.unit_active(record["port"]) else None
        head = self.head()
        port, adopted = self.pick_port(active_port, head, record["warming"] if record else ())
        if port is None:
            log(f"roll ({reason}) skipped: every backend port {self.ports} is busy; retiring backends hold them")
            return False
        # Port discovery and marker waits may outlive the first admission observation.
        # Do not publish warming state (or consume the unit boot budget) under a new hold.
        if self.admission_deferred(reason):
            return False
        log(f"roll ({reason}): {'adopting' if adopted else 'starting'} backend :{port}"
            f" (active :{active_port or '-'}, HEAD {(head or '?')[:10]})")
        if active_port is None:
            write_active(port, home=self.home, reason=reason)  # nothing serves: point the proxy at it now
        else:
            write_active(active_port, warming=[port], home=self.home, reason=reason)
        if not adopted:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self.systemctl("reset-failed", backend_unit(port), timeout=15)
            started = self.systemctl("start", "--no-block", backend_unit(port), timeout=30)
            if started.returncode != 0:
                log(f"roll: could not start {backend_unit(port)}: {(started.stderr or '').strip()[-200:]}")
                self._abandon(port, active_port)
                return False
        body = self._await_health(port)
        if body is None:
            log(f"roll: backend :{port} did not answer within {self.boot_timeout_s:.0f}s; keeping"
                f" :{active_port or '-'}")
            self._abandon(port, active_port)
            return False
        paths = self.replay_paths(record.get("pid") if record else None)
        paths += [p for p in self.board_paths(port) if p not in paths]
        warm = self.warm(port, paths)
        body = self.health(port) if self.unit_active(port) else None  # it may have died while it warmed
        if body is None:
            log(f"roll: backend :{port} stopped answering while it warmed; keeping :{active_port or '-'}")
            self._abandon(port, active_port)
            return False
        write_active(port, home=self.home, pid=self.unit_pid(port), commit=body.get("commit") or "",
                     reason=reason, warm=warm)
        log(f"roll: switched to :{port} (commit {str(body.get('commit') or '?')[:10]}); warm {warm};"
            f" :{active_port or '-'} retires when idle")
        return True

    def _abandon(self, port: int, active_port: Optional[int]) -> None:
        if active_port is not None:
            write_active(active_port, home=self.home)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self.systemctl("stop", "--no-block", backend_unit(port), timeout=15)  # never served a client

    def _await_health(self, port: int) -> Optional[dict]:
        deadline = self.clock() + self.boot_timeout_s
        while self.clock() < deadline:
            body = self.health(port)
            if body is not None:
                return body
            self.sleep(1.0)
        return None

    # -- warming --
    def board_paths(self, port: int) -> list[str]:
        """GET /board for every unarchived board the successor lists. Desktop reads the rarely-polled boards
        on reconnect, and board reads share 2 admission slots, so one cold board queues the warm ones behind
        its first read (live 18:41Z: board=default waited 18-22 s). Best effort: no list, no extra paths."""
        try:
            status, _elapsed, body = self.get(port, "/api/plugins/kanban/boards", WARM_REQUEST_TIMEOUT_S)
        except Exception:  # health: allow BLE001 -- warming is best effort: no list, no extra paths
            return []
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("boards"), list):
            return []
        out: list[str] = []
        for board in body["boards"]:
            slug = board.get("slug") if isinstance(board, dict) else None
            if not isinstance(slug, str) or not slug or board.get("archived"):
                continue
            url = "/api/plugins/kanban/board?board=" + urllib.parse.quote(slug, safe="")
            if url not in out:
                out.append(url)
        return out

    def replay_paths(self, old_pid: object) -> list[str]:
        """The default reads plus the allowlisted GETs (with their queries) the old backend served in the
        last two hours, from loop-watch's request_stats. Best effort: no log, no replays."""
        paths: list[str] = list(DEFAULT_WARM)
        counts: dict[str, int] = {}
        for row in _tail_jsonl(self.loop_watch, LOOP_WATCH_TAIL_BYTES):
            if row.get("kind") != "request_stats" or (old_pid and row.get("pid") != old_pid):
                continue
            try:
                if time.time() - float(row.get("hour_epoch") or 0) > 7200:
                    continue
            except (TypeError, ValueError):
                continue
            for path, stats in (row.get("paths") or {}).items():
                if path not in REPLAY_PATHS or not isinstance(stats, dict):
                    continue
                for query, n in stats.get("queries") or ():
                    if isinstance(query, str) and isinstance(n, int) and len(query) < 2000:
                        url = f"{path}?{query}" if query else path
                        counts[url] = counts.get(url, 0) + n
        for url, _n in sorted(counts.items(), key=lambda kv: -kv[1])[:WARM_MAX_REPLAYS]:
            if url not in paths:
                paths.append(url)
        return paths

    def warm(self, port: int, paths: list[str]) -> dict:
        """Two rounds of the paths, concurrently, inside ``warm_cap_s``: the first pays the cold caches, the
        second shows what Desktop will see. Returns a summary for the log."""
        deadline = self.clock() + self.warm_cap_s
        summary: dict = {"paths": len(paths)}
        with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
            for round_no in (1, 2):
                left = deadline - self.clock()
                if left <= 0:
                    summary[f"round{round_no}"] = "skipped (cap)"
                    break
                timeout = min(WARM_REQUEST_TIMEOUT_S, left)
                futures = [pool.submit(self.get, port, path, timeout) for path in paths]
                results = []
                for future in futures:
                    with contextlib.suppress(Exception):
                        results.append(future.result(timeout=max(0.1, deadline - self.clock())))
                times = sorted(elapsed for _status, elapsed, _body in results)
                summary[f"round{round_no}"] = {"done": len(results), "max_s": round(times[-1], 2) if times else None,
                                               "errors": sum(1 for status, *_ in results if status != 200)}
        return summary

    # -- supervision --
    def drift(self) -> Optional[str]:
        """Why the active backend should be replaced now, or None."""
        record = read_active(self.home)
        if record is None:
            return "no active backend recorded"
        port = record["port"]
        if not self.unit_active(port):
            return f"active backend :{port} is not running"
        if self.update_in_progress():
            return None
        body = self.health(port)
        if body is None:
            # Running but not answering (a starved threadpool, 2026-11-21): every Desktop call through the proxy
            # times out until a roll moves new connections to a fresh backend.
            return f"active backend :{port} {UNRESPONSIVE_REASON}"
        head = self.head()
        if self.same_commit(body.get("commit"), head) is False:
            return f"active backend :{port} runs {str(body.get('commit'))[:10]}, checkout is at {str(head)[:10]}"
        if self.runtime_intact(port) is False:
            return f"active backend :{port} lost its runtime (runtimeIntact false)"
        return None

    def _superseded_path(self) -> Path:
        return aod.state_dir(self.home) / SUPERSEDED_NAME

    def _load_superseded(self) -> dict[int, float]:
        """{port: wall time first seen superseded}. Persisted, because the roller itself restarts on every
        core update and an in-memory clock would never reach the grace."""
        with contextlib.suppress(OSError, ValueError, TypeError):
            data = json.loads(self._superseded_path().read_text(encoding="utf-8-sig"))
            return {int(k): float(v) for k, v in data.items()}
        return {}

    def _demoted_path(self) -> Path:
        return aod.state_dir(self.home) / DEMOTED_NAME

    def _load_demoted(self) -> dict[int, float]:
        """{port: wall time it was rolled away from for not answering}. Persisted like superseded.json."""
        with contextlib.suppress(OSError, ValueError, TypeError):
            data = json.loads(self._demoted_path().read_text(encoding="utf-8-sig"))
            return {int(k): float(v) for k, v in data.items()}
        return {}

    def _save_demoted(self, since: dict[int, float]) -> None:
        with contextlib.suppress(OSError):
            aod._write_private(self._demoted_path(), json.dumps({str(k): v for k, v in since.items()}))

    def demote_if_replaced(self, port: int) -> bool:
        """Demote *port* after a roll away from it. A backend that exited between the drift poll and the roll can
        have its freed port reused by the replacement: the port active now is never demoted."""
        after = read_active(self.home)
        if after is None or after["port"] == int(port):
            return False
        self.demote(port)
        return True

    def _never_answers(self, port: int) -> bool:
        for i in range(UNRESPONSIVE_CONFIRMATIONS):
            if i:
                self.sleep(START_PROBE_GAP_S)
            if self.health(port) is not None:
                return False
        return True

    def start(self) -> bool:
        """The roller's first roll. A starved active backend found at start (a core update restarts the roller)
        is demoted like one supervise() rolls away from, so it is not adopted again or kept for 6 h. Decided after
        the update marker clears (a backend paused for the update is not starved) and only when every one of
        UNRESPONSIVE_CONFIRMATIONS probes misses, the same bar supervise() uses."""
        cleared = aod.wait_for_update_clear(self.marker, sleep=self.sleep, now=self.clock)
        if not cleared:
            log("update marker still present; rolling anyway")
        before = read_active(self.home)
        starved = before is not None and self.unit_active(before["port"]) and self._never_answers(before["port"])
        # A marker that cleared may be taken again by a new update during the probes: roll() checks again then.
        # Only a stale (timed-out) marker skips roll()'s wait, so it holds the start once, not twice.
        ok = self.roll("roller started", marker_waited=not cleared)
        if ok and starved:
            self.demote_if_replaced(before["port"])
        return ok

    def demote(self, port: int) -> None:
        demoted = self._load_demoted()
        demoted.setdefault(int(port), self.wall())
        self._save_demoted(demoted)

    def _save_superseded(self, since: dict[int, float]) -> None:
        with contextlib.suppress(OSError):
            aod._write_private(self._superseded_path(), json.dumps({str(k): v for k, v in since.items()}))

    def _retire_fenced(self, port: int) -> bool:
        """Retire *port* through its own idle fence (``/api/health/retirement``), as its watchdog would: only
        a backend proven idle is committed and then stopped. A live turn refuses the prepare."""
        permit = self.post(port, "/api/health/retirement", {"action": "prepare"}) or {}
        if permit.get("ok") is not True or not isinstance(permit.get("token"), str):
            return False
        commit = self.post(port, "/api/health/retirement", {"action": "commit", "token": permit["token"]}) or {}
        if commit.get("ok") is not True:
            return False
        try:
            return self.systemctl("stop", "--no-block", backend_unit(port), timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False  # keep the original superseded age so the next poll retries

    def reap_superseded(self, grace_s: float = SUPERSEDED_GRACE_S, hard_s: float = SUPERSEDED_HARD_S) -> list[int]:
        """Backstop for backends that are neither active nor warming and have not retired themselves (a core
        without the superseded watchdog, a lost watchdog thread). After *grace_s* the roller retires one
        through its idle fence, so a live turn is never cut. Only after *hard_s*, a backend that has stayed
        busy that long is stopped (SIGTERM checkpoints its turns) so the port pool cannot leak forever."""
        record = read_active(self.home)
        if record is None:
            return []
        live = {record["port"], *record["warming"]}
        since = self._load_superseded()
        demoted = self._load_demoted()
        now, stopped = self.wall(), []
        for port in self.ports:
            if port in live or not self.unit_active(port):
                since.pop(port, None)
                demoted.pop(port, None)
                continue
            age = now - since.setdefault(port, now)
            if port in demoted and now - demoted[port] >= min(hard_s, DEMOTED_HARD_S):
                log(f"backend :{port} demoted {now - demoted[port]:.0f}s ago for not answering; stopping it")
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self.systemctl("stop", "--no-block", backend_unit(port), timeout=15)
            elif age >= hard_s:
                log(f"backend :{port} still busy {age:.0f}s after it was superseded; stopping it")
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self.systemctl("stop", "--no-block", backend_unit(port), timeout=15)
            elif age >= grace_s and self._retire_fenced(port):
                log(f"backend :{port} superseded {age:.0f}s ago and idle; retired it through its fence")
            else:
                continue
            since.pop(port, None)
            demoted.pop(port, None)
            stopped.append(port)
        self._save_superseded(since)
        self._save_demoted(demoted)
        return stopped

    def supervise(self, *, max_polls: Optional[int] = None) -> None:
        seen, kind, polls, retry_at = 0, None, 0, 0.0
        while max_polls is None or polls < max_polls:
            polls += 1
            try:
                self.reap_superseded()
                reason = self.drift()
                dead = bool(reason) and "not running" in reason or reason == "no active backend recorded"
                unresponsive = bool(reason) and UNRESPONSIVE_REASON in reason
                # Each kind confirms on its own count: polls of one kind never complete the other's.
                now_kind = ("unresponsive" if unresponsive else "drift") if reason else None
                seen = seen + 1 if reason and now_kind == kind else (1 if reason else 0)
                kind = now_kind
                need = UNRESPONSIVE_CONFIRMATIONS if unresponsive else DRIFT_CONFIRMATIONS
                if reason and (dead or seen >= need) and self.clock() >= retry_at:
                    seen = 0
                    before = read_active(self.home)
                    if not self.roll(reason):
                        retry_at = self.clock() + FAILED_ROLL_BACKOFF_S
                    elif unresponsive and before is not None:
                        self.demote_if_replaced(before["port"])
            # One bad poll (a malformed answer, a systemctl hiccup) must not end supervision.
            except Exception:  # health: allow BLE001 -- supervisor loop boundary; traceback logged
                log(f"supervise: poll failed:\n{traceback.format_exc()}")
            self.sleep(SUPERVISE_POLL_S)


def _port_listening(port: int) -> bool:
    with contextlib.suppress(OSError):
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    return False


def _tail_jsonl(path: Optional[Path], max_bytes: int) -> list[dict]:
    if path is None:
        return []
    rows = []
    with contextlib.suppress(OSError):
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
        for line in data.splitlines()[1 if size > max_bytes else 0:]:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def default_checkout(hermes_home: Path) -> Optional[Path]:
    path = hermes_home / "hermes-agent"
    return path if (path / ".git").exists() else None


def cmd_roll(args) -> int:
    """``hermes-always-on-dashboard.service`` (the roller): roll once, then supervise. SIGTERM (a restart)
    exits at once and leaves every backend running: the next roller adopts or supersedes them."""
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    hermes_home = aod._root_hermes_home()
    token, _nonce = aod.ensure_credentials()
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _systemctl("start", "--no-block", PROXY_UNIT, timeout=30)
    roller = Roller(token=token, checkout=default_checkout(hermes_home),
                    marker=hermes_home / ".hermes-update-in-progress",
                    loop_watch=hermes_home / "logs" / "loop-watch.jsonl")
    roller.start()
    roller.supervise()
    return 0


# ── unit rendering (Linux) ───────────────────────────────────────────────────────────────────────────


def render_units(*, exec_prefix: list[str], hermes: str, port: int, log_path: Path, path_env: str) -> dict[str, str]:
    """{unit file name: text} for the proxy, the backend template and the roller. *exec_prefix* runs this
    module's CLI: ``[hermes, "always-on-dashboard"]`` or ``[python3, always_on_dashboard.py]``."""
    import shlex

    def exec_line(*tail: str) -> str:
        return "ExecStart=" + " ".join(shlex.quote(x) for x in [*exec_prefix, *tail])

    common = [f"Environment=PATH={path_env}", f"StandardOutput=append:{log_path}",
              f"StandardError=append:{log_path}"]
    proxy = ["[Unit]", "Description=Hermes always-on dashboard: stable loopback port for Desktop over SSH",
             "After=network-online.target", "", "[Service]", "Type=simple",
             exec_line("proxy", "--port", str(int(port)), "--hermes", hermes),
             "Restart=always", "RestartSec=1", *common, "", "[Install]", "WantedBy=default.target", ""]
    backend = ["[Unit]", "Description=Hermes always-on dashboard backend on port %i", "", "[Service]",
               "Type=simple", exec_line("backend", "--port", "%i", "--hermes", hermes),
               # The roller replaces a dead backend warm; a unit restart here would come back cold.
               "Restart=no",
               # SIGTERM runs the server's flush-on-kill handlers; give a turn's checkpoint time to land.
               "TimeoutStopSec=60", *common, ""]
    roller = ["[Unit]", "Description=Hermes always-on remote dashboard (warm backend for Hermes Desktop over SSH)",
              f"Wants={PROXY_UNIT}", f"After=network-online.target {PROXY_UNIT}", "", "[Service]",
              # simple: `systemctl restart` returns at once; the roll (boot + warm) runs after it.
              "Type=simple", exec_line("run", "--port", str(int(port)), "--hermes", hermes),
              "Restart=always", "RestartSec=5", "TimeoutStopSec=10", *common, "",
              "[Install]", "WantedBy=default.target", ""]
    return {PROXY_UNIT: "\n".join(proxy), BACKEND_TEMPLATE: "\n".join(backend), ROLLER_UNIT: "\n".join(roller)}


def install_units(units: dict[str, str], unit_dir: Path, run: Callable = aod._run) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (unit_dir / name).write_text(text, encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", PROXY_UNIT, ROLLER_UNIT])
    run(["systemctl", "--user", "start", PROXY_UNIT], check=False)
    # A pre-proxy install's serve still owns the port: restarting the roller stops it, and the proxy's
    # bind retries until it lets go.
    run(["systemctl", "--user", "restart", ROLLER_UNIT])


def stop_all(run: Callable = aod._run) -> None:
    run(["systemctl", "--user", "disable", "--now", ROLLER_UNIT, PROXY_UNIT], check=False)
    run(["systemctl", "--user", "stop", "hermes-always-on-backend@*.service"], check=False)
    with contextlib.suppress(OSError):
        active_path().unlink()


__all__ = [
    "BACKEND_PORTS", "ENV_BACKEND", "PROXY_UNIT", "Proxy", "Roller", "backend_port_for_pid", "bind_listener",
    "cmd_backend", "cmd_proxy", "cmd_roll", "handoff_control_read", "install_units", "is_superseded",
    "model_options_control_path",
    "pop_backend_port", "read_active", "render_units", "request_roll", "route_always_on_backends",
    "stop_all", "successor_pending", "this_backend_port", "verify_backend_custody", "write_active",
    "write_backend_custody",
]
