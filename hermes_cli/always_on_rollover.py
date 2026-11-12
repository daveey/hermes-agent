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
  (``web_server_skew_exit.start_superseded_watchdog``), so live turns are never cut by a roll.
* ``hermes-always-on-dashboard.service`` (the roller, ``Type=simple``, so a restart returns at once):
  it starts a backend on a free port, waits for it to answer, warms it by replaying the reads Desktop
  actually made, then switches ``active.json``. Afterwards it watches the active backend: a dead
  backend, a commit behind the checkout HEAD, or a replaced runtime starts another roll.

``hermes update`` no longer kills a ``backend@`` pid. It asks the roller to roll instead
(``route_always_on_backends``, called from ``dashboard_procs._kill_stale_dashboard_processes``), because a cold restart of the active backend is exactly
what this replaces. Everything here is stdlib only: the hub runs this file with the system python3,
next to an interim copy of ``always_on_dashboard.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
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

PROXY_HOLD_S = 30.0          # a client waits this long for an answering backend before its connection is closed
PROXY_CONNECT_TIMEOUT_S = 5.0
BOOT_TIMEOUT_S = 180.0       # a new backend that does not answer /api/health by then is given up (old one stays)
WARM_CAP_S = 75.0            # the warm rounds stop here; the backend answers, so it is switched in anyway
WARM_REQUEST_TIMEOUT_S = 60.0
WARM_WORKERS = 6
WARM_MAX_REPLAYS = 40
SUPERVISE_POLL_S = 15.0
DRIFT_CONFIRMATIONS = 2      # consecutive polls a commit/runtime drift must persist before it rolls
FAILED_ROLL_BACKOFF_S = 300.0
SUPERSEDED_GRACE_S = 900.0   # a superseded backend that has not retired itself by then is stopped (SIGTERM)
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
    raw = environ.pop(ENV_BACKEND, "")
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else None


def request_roll(run: Callable = subprocess.run) -> bool:
    """Ask the roller unit for a warm roll without waiting for it (``hermes update``'s dashboard refresh)."""
    try:
        return run(["systemctl", "--user", "restart", "--no-block", ROLLER_UNIT], capture_output=True,
                   text=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
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
    if not (pids and restart_managed and sys.platform.startswith("linux")):
        return pids
    return route_always_on_backends(pids)


# ── the proxy ────────────────────────────────────────────────────────────────────────────────────────


class Proxy:
    """Splices each accepted connection to the active backend. The target is read per connection, so a
    switch moves new connections only; existing ones stay on the backend that accepted them."""

    def __init__(self, *, home: Optional[Path] = None, hold_s: float = PROXY_HOLD_S,
                 clock: Callable[[], float] = time.monotonic):
        self.home, self.hold_s, self.clock = home, hold_s, clock
        self._cached: tuple[Optional[int], Optional[int]] = (None, None)  # (mtime_ns, port)
        self.last_port: Optional[int] = None

    def target(self) -> Optional[int]:
        try:
            mtime = active_path(self.home).stat().st_mtime_ns
        except OSError:
            return None
        if self._cached[0] != mtime:
            record = read_active(self.home)
            self._cached = (mtime, record["port"] if record else None)
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
        await asyncio.gather(_pipe(client_reader, up_writer), _pipe(up_reader, client_writer))


async def _pipe(reader, writer) -> None:
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
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            writer.close()


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


def cmd_backend(args) -> int:
    """``hermes-always-on-backend@<port>``: stage the persisted token and exec the isolated SSH serve."""
    hermes = os.path.abspath(args.hermes)
    token, nonce = aod.ensure_credentials()
    token_path = aod.stage_token_file(token, nonce)
    env = dict(os.environ)
    env["HERMES_DESKTOP"] = "1"
    env[aod.ENV_FLAG] = "1"
    env[ENV_BACKEND] = str(int(args.port))
    env.pop("HERMES_DASHBOARD_SESSION_TOKEN", None)
    argv = aod.build_serve_argv(hermes, args.port, token_path, nonce)
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
                 get: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, loop_watch: Optional[Path] = None,
                 ports: tuple[int, ...] = BACKEND_PORTS, boot_timeout_s: float = BOOT_TIMEOUT_S,
                 warm_cap_s: float = WARM_CAP_S):
        self.token, self.checkout, self.marker, self.home = token, checkout, marker, home
        self.systemctl, self.clock, self.sleep, self.ports = systemctl, clock, sleep, ports
        self.get = get or self._http_get
        self.loop_watch = loop_watch
        self.boot_timeout_s, self.warm_cap_s = boot_timeout_s, warm_cap_s
        self.superseded_since: dict[int, float] = {}

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
        except (OSError, ValueError):
            return None, time.monotonic() - started, None
        parsed = None
        with contextlib.suppress(ValueError):
            parsed = json.loads(body) if body[:1] in (b"{", b"[") else None
        return status, time.monotonic() - started, parsed if isinstance(parsed, dict) else None

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
    def pick_port(self, active_port: Optional[int], head: Optional[str]) -> tuple[Optional[int], bool]:
        """(port, adopted). Adopt a non-active backend already answering on HEAD (a roll a restart cut
        short); else the first port with no unit; else None."""
        others = [p for p in self.ports if p != active_port]
        running = [p for p in others if self.unit_active(p)]
        for port in running:
            body = self.health(port)
            if body and self.same_commit(body.get("commit"), head):
                return port, True
        for port in others:
            if port not in running and not _port_listening(port):
                return port, False
        return None, False

    def roll(self, reason: str) -> bool:
        if not aod.wait_for_update_clear(self.marker, sleep=self.sleep, now=self.clock):
            log("update marker still present; rolling anyway")
        record = read_active(self.home)
        active_port = record["port"] if record and self.unit_active(record["port"]) else None
        head = self.head()
        port, adopted = self.pick_port(active_port, head)
        if port is None:
            log(f"roll ({reason}) skipped: every backend port {self.ports} is busy; retiring backends hold them")
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
        warm = self.warm(port, self.replay_paths(record.get("pid") if record else None))
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
    def replay_paths(self, old_pid: object) -> list[str]:
        """The default reads plus the allowlisted GETs (with their queries) the old backend served in the
        last hour, from loop-watch's request_stats. Best effort: no log, no replays."""
        paths: list[str] = list(DEFAULT_WARM)
        counts: dict[str, int] = {}
        for row in _tail_jsonl(self.loop_watch, LOOP_WATCH_TAIL_BYTES):
            if row.get("kind") != "request_stats" or (old_pid and row.get("pid") != old_pid):
                continue
            if time.time() - float(row.get("hour_epoch") or 0) > 7200:
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
        head = self.head()
        if body and self.same_commit(body.get("commit"), head) is False:
            return f"active backend :{port} runs {str(body.get('commit'))[:10]}, checkout is at {str(head)[:10]}"
        if body and self.runtime_intact(port) is False:
            return f"active backend :{port} lost its runtime (runtimeIntact false)"
        return None

    def reap_superseded(self, grace_s: float = SUPERSEDED_GRACE_S) -> list[int]:
        """Stop backends that are neither active nor warming once they have outlived *grace_s* since this roller
        first saw them superseded. They normally retire themselves when idle (the superseded watchdog); this is
        the backstop for one that never goes idle, or a core without that watchdog. SIGTERM checkpoints turns
        exactly as the old in-place restart did, only later."""
        record = read_active(self.home)
        if record is None:
            return []
        live = {record["port"], *record["warming"]}
        now, stopped = self.clock(), []
        for port in self.ports:
            if port in live or not self.unit_active(port):
                self.superseded_since.pop(port, None)
                continue
            first = self.superseded_since.setdefault(port, now)
            if now - first >= grace_s:
                log(f"backend :{port} still running {now - first:.0f}s after it was superseded; stopping it")
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self.systemctl("stop", "--no-block", backend_unit(port), timeout=15)
                self.superseded_since.pop(port, None)
                stopped.append(port)
        return stopped

    def supervise(self, *, max_polls: Optional[int] = None) -> None:
        seen, polls, retry_at = 0, 0, 0.0
        while max_polls is None or polls < max_polls:
            polls += 1
            self.reap_superseded()
            reason = self.drift()
            dead = bool(reason) and "not running" in reason or reason == "no active backend recorded"
            seen = seen + 1 if reason else 0
            if reason and (dead or seen >= DRIFT_CONFIRMATIONS) and self.clock() >= retry_at:
                seen = 0
                if not self.roll(reason):
                    retry_at = self.clock() + FAILED_ROLL_BACKOFF_S
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
    roller.roll("roller started")
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
    "cmd_backend", "cmd_proxy", "cmd_roll", "install_units", "is_superseded", "pop_backend_port",
    "read_active", "render_units", "request_roll", "route_always_on_backends", "stop_all", "write_active",
]
