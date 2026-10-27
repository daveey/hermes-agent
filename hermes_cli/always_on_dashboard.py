"""Always-on remote dashboard: one supervised ``hermes serve`` per host for Hermes Desktop over SSH.

Desktop reaches a remote gateway by SSH and, today, spawns a detached ``serve --isolated`` there on
first use (about 8 s: platform probes, Python boot, READY scrape). That backend exits 900 s after its
last client and retires itself after a core update, so most opens after a pause pay the spawn again.

This module keeps one backend warm instead. ``hermes always-on-dashboard install`` writes a launchd
agent (macOS) or a systemd user unit (Linux) that runs ``hermes always-on-dashboard run``. ``run``
execs the SAME isolated SSH backend Desktop would spawn (``HERMES_DESKTOP=1 serve --isolated
--ssh-session-token-file ... --ssh-owner-nonce ...``: same auth gate, ownership proof, cron gate and
code-skew retirement), with three differences:

* the token and owner nonce persist in ``~/.hermes/desktop-ssh/always-on/`` (0700 dir, 0600 files),
  so every restart serves the same credential and Desktop reads it over SSH instead of minting one;
* the port is fixed, so a second copy exits ``PORT_IN_USE`` and one host never runs two;
* ``HERMES_ALWAYS_ON_DASHBOARD=1`` turns OFF the idle-exit watchdog (``web_server_idle_exit``) for
  this process only. Everything else about turns is unchanged.

After a core update the existing code-skew watchdog exits it between turns and the supervisor starts
it again on the new code (``run`` first waits for the update marker to clear). ``record.json`` names
the pid/port/nonce; Desktop's ``remote-lifecycle.connect()`` checks it before the lockfile path.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Callable, Optional

ENV_FLAG = "HERMES_ALWAYS_ON_DASHBOARD"
DEFAULT_PORT = 9147
RECORD_SCHEMA = 1
LAUNCHD_LABEL = "com.hermes.always-on-dashboard"
SYSTEMD_UNIT = "hermes-always-on-dashboard.service"
# The token file `serve --ssh-session-token-file` accepts must sit at desktop-ssh/<32 hex>/<16 hex>.token.
# A fixed slot id keeps it apart from every Desktop ownership slot (those are per-install random ids).
OWNERSHIP_ID = hashlib.sha256(b"hermes-always-on-dashboard").hexdigest()[:32]
UPDATE_WAIT_S = 600.0

_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_NONCE_RE = re.compile(r"[0-9a-f]{16}")


def desktop_ssh_root(home: Optional[Path] = None) -> Path:
    """``$HOME/.hermes/desktop-ssh``: where Desktop's SSH runtime lives, independent of HERMES_HOME."""
    return (home or Path.home()) / ".hermes" / "desktop-ssh"


def state_dir(home: Optional[Path] = None) -> Path:
    return desktop_ssh_root(home) / "always-on"


def always_on_enabled() -> bool:
    """True in the always-on backend process (read once by the web server; not inherited further)."""
    return os.environ.get(ENV_FLAG) == "1"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(tmp, path)


def _read_matching(path: Path, pattern: re.Pattern) -> str:
    with contextlib.suppress(OSError):
        value = path.read_text(encoding="utf-8").strip()
        if pattern.fullmatch(value):
            return value
    return ""


def ensure_credentials(home: Optional[Path] = None) -> tuple[str, str]:
    """The persisted (token, nonce), minted on first use. Never rotated by a restart."""
    _private_dir(desktop_ssh_root(home))
    directory = state_dir(home)
    _private_dir(directory)
    token = _read_matching(directory / "token", _TOKEN_RE)
    if not token:
        token = secrets.token_hex(32)
        _write_private(directory / "token", token)
    nonce = _read_matching(directory / "nonce", _NONCE_RE)
    if not nonce:
        nonce = secrets.token_hex(8)
        _write_private(directory / "nonce", nonce)
    return token, nonce


def stage_token_file(token: str, nonce: str, home: Optional[Path] = None) -> Path:
    """Write the one-shot token file ``serve`` reads and unlinks, in the shape it validates."""
    slot = desktop_ssh_root(home) / OWNERSHIP_ID
    _private_dir(slot)
    path = slot / f"{nonce}.token"
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    _write_private(path, token)
    return path


def write_record(*, pid: int, port: int, nonce: str, hermes_home: str, hermes_path: str,
                 home: Optional[Path] = None) -> Path:
    record = {
        "schemaVersion": RECORD_SCHEMA,
        "pid": int(pid),
        "port": int(port),
        "spawnNonce": nonce,
        "hermesHome": hermes_home,
        "hermesPath": hermes_path,
        "startedAt": datetime.now(UTC).isoformat(),
    }
    path = state_dir(home) / "record.json"
    _write_private(path, json.dumps(record))
    return path


def wait_for_update_clear(marker: Path, *, timeout_s: float = UPDATE_WAIT_S,
                          sleep: Callable[[float], None] = time.sleep,
                          now: Callable[[], float] = time.monotonic) -> bool:
    """Block while ``hermes update`` holds its marker so a restart never boots half-written code.
    True when clear; False after ``timeout_s`` (the caller starts anyway: a stale marker must not
    keep the backend down forever)."""
    deadline = now() + timeout_s
    while marker.exists():
        if now() >= deadline:
            return False
        sleep(2.0)
    return True


def build_serve_argv(hermes: str, port: int, token_path: Path, nonce: str) -> list[str]:
    return [hermes, "serve", "--isolated", "--host", "127.0.0.1", "--port", str(int(port)),
            "--ssh-session-token-file", str(token_path), "--ssh-owner-nonce", nonce]


def _root_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def cmd_run(args: argparse.Namespace) -> int:
    """Supervisor entry point: wait out an update, stage the token, publish the record, exec serve."""
    from hermes_cli.update_lock import MARKER_NAME

    hermes = os.path.abspath(args.hermes or sys.argv[0])
    hermes_home = _root_hermes_home()
    if not wait_for_update_clear(hermes_home / MARKER_NAME):
        print(f"always-on dashboard: update marker still present after {UPDATE_WAIT_S:.0f}s; starting anyway",
              file=sys.stderr, flush=True)
    token, nonce = ensure_credentials()
    token_path = stage_token_file(token, nonce)
    # exec keeps this pid, so the record names the serve process itself.
    write_record(pid=os.getpid(), port=args.port, nonce=nonce, hermes_home=str(hermes_home), hermes_path=hermes)
    env = dict(os.environ)
    env["HERMES_DESKTOP"] = "1"
    env[ENV_FLAG] = "1"
    env.pop("HERMES_DASHBOARD_SESSION_TOKEN", None)
    argv = build_serve_argv(hermes, args.port, token_path, nonce)
    print(f"always-on dashboard: exec {shlex.join(argv[:7])} ...", file=sys.stderr, flush=True)
    os.execve(hermes, argv, env)
    return 0  # unreachable


def _service_path(hermes: str) -> str:
    parts = [os.path.dirname(hermes), str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin",
             "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(parts))


def render_launchd_plist(*, hermes: str, port: int, home: Path, log_path: Path) -> bytes:
    """KeepAlive: launchd restarts it after any exit (crash, code-skew retirement after an update)."""
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [hermes, "always-on-dashboard", "run", "--port", str(int(port)), "--hermes", hermes],
        "EnvironmentVariables": {"HOME": str(home), "PATH": _service_path(hermes), "LANG": "C.UTF-8"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "WorkingDirectory": str(home),
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    })


def render_systemd_unit(*, hermes: str, port: int, log_path: Path) -> str:
    """Restart=always: systemd restarts it after any exit, including a clean code-skew retirement."""
    return "\n".join([
        "[Unit]",
        "Description=Hermes always-on remote dashboard (warm backend for Hermes Desktop over SSH)",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={shlex.quote(hermes)} always-on-dashboard run --port {int(port)} --hermes {shlex.quote(hermes)}",
        f"Environment=PATH={_service_path(hermes)}",
        "Restart=always",
        "RestartSec=5",
        # SIGTERM runs the server's flush-on-kill handlers; give a turn's checkpoint time to land.
        "TimeoutStopSec=60",
        f"StandardOutput=append:{log_path}",
        f"StandardError=append:{log_path}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def _log_path() -> Path:
    path = _root_hermes_home() / "logs" / "always-on-dashboard.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def cmd_install(args: argparse.Namespace) -> int:
    hermes = os.path.abspath(args.hermes or sys.argv[0])
    home = Path.home()
    ensure_credentials()
    if sys.platform == "darwin":
        plist = _launchd_plist_path(home)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(render_launchd_plist(hermes=hermes, port=args.port, home=home, log_path=_log_path()))
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
        _run(["launchctl", "bootstrap", domain, str(plist)])
        print(f"installed {plist} (launchd {domain}/{LAUNCHD_LABEL}, port {args.port})")
    elif sys.platform.startswith("linux"):
        unit = _systemd_unit_path(home)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(render_systemd_unit(hermes=hermes, port=args.port, log_path=_log_path()), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        print(f"installed {unit} (systemd --user {SYSTEMD_UNIT}, port {args.port})")
    else:
        print("always-on dashboard supervision is macOS (launchd) and Linux (systemd) only", file=sys.stderr)
        return 2
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    home = Path.home()
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        _launchd_plist_path(home).unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
        _systemd_unit_path(home).unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    (state_dir() / "record.json").unlink(missing_ok=True)
    print("always-on dashboard uninstalled (token kept; delete ~/.hermes/desktop-ssh/always-on to rotate it)")
    return 0


def read_record(home: Optional[Path] = None) -> Optional[dict]:
    with contextlib.suppress(OSError, ValueError):
        record = json.loads((state_dir(home) / "record.json").read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("schemaVersion") == RECORD_SCHEMA:
            return record
    return None


def cmd_status(args: argparse.Namespace) -> int:
    record = read_record()
    if not record:
        print("always-on dashboard: no record (not installed or not started yet)")
        return 1
    alive = False
    with contextlib.suppress(OSError):
        os.kill(int(record["pid"]), 0)
        alive = True
    print(json.dumps({**record, "alive": alive}))
    return 0 if alive else 1


def build_always_on_dashboard_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "always-on-dashboard",
        help="Keep one supervised backend warm for Hermes Desktop SSH connections",
        description="Install, run or inspect the always-on remote dashboard: a launchd/systemd-supervised "
            "`hermes serve` that Hermes Desktop reuses over SSH instead of spawning a backend per connect.")
    sub = parser.add_subparsers(dest="always_on_action", required=True)
    for name, func, help_text in (
        ("install", cmd_install, "Write and load the launchd agent / systemd user unit"),
        ("uninstall", cmd_uninstall, "Stop and remove the supervisor unit"),
        ("status", cmd_status, "Print the running backend's record"),
        ("run", cmd_run, "Supervisor entry point (execs `hermes serve`)"),
    ):
        action = sub.add_parser(name, help=help_text)
        if name in ("install", "run"):
            action.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Loopback port (default {DEFAULT_PORT})")
            action.add_argument("--hermes", default="", help="Absolute path of the hermes entry point to exec")
        action.set_defaults(func=func)
