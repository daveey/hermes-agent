"""Gateway capability registry: what this gateway can do, live-checked, shared via hermes-hub. [daveey]

A *capability* is a namespaced, secret-free name for something a gateway can do on behalf of any
card, whatever profile runs it: ``aws:767406518141:admin``, ``github:daveey:repo``,
``ssh:mini``, ``sudo:nopasswd``, ``token-broker:user/cf-dbloom-infra``, ``browser:signed-in``.
Each one is held only while its probe (a cheap shell command: ``aws sts get-caller-identity
--profile …``, ``gh auth status``, ``ssh -o BatchMode=yes <host> true``) exits 0, and every
entry carries the time it was last checked.

This gateway writes ``<kanban home>/kanban/capabilities.json``. The hermes-hub client publishes
that file as its ``capabilities`` record (names, ok, checked_at; never commands or output), so
every gateway's replica (``~/.local/state/hermes-hub/hub.db``) knows who holds what. Routing
(:mod:`hermes_cli.kanban_capability_routing`) reads it and only trusts entries checked within
``stale_after_seconds``: stale data never routes.

Config (``config.yaml``, default profile of the gateway)::

    kanban:
      capabilities:
        probes:                       # name -> shell command; exit 0 = held
          "aws:767406518141:admin": "aws sts get-caller-identity --profile david-bloomin-sso"
        ssh_hosts:                    # ssh:<name> held while BatchMode ssh to the host works
          hub: ubuntu@100.104.201.88
        builtin: true                 # sudo:nopasswd and github:<login>[:<scope>]
        refresh_seconds: 600
        stale_after_seconds: 1800
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

SCHEMA = 1
NEED_RE = re.compile(r"[a-z][a-z0-9-]*(?::[A-Za-z0-9._@/+-]+)+")
COVERING_LEAF = "admin"
CONFIG_TTL = 60
DEFAULT_REFRESH_SECONDS = 600
DEFAULT_STALE_AFTER_SECONDS = 1800
PROBE_TIMEOUT = 20
MAX_NAME_LEN = 160

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


# --- names ---------------------------------------------------------------------------------

def is_gateway_need(value) -> bool:
    """A namespaced capability name (``ns:part[:part…]``), never a profile tool name."""
    return isinstance(value, str) and len(value) <= MAX_NAME_LEN and bool(NEED_RE.fullmatch(value))


def normalize_needs(value) -> list[str]:
    """Validated, deduplicated, order-preserving list of capability needs (ValueError if bad)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("needs must be a list of capability names")
    out: list[str] = []
    for item in value:
        if not is_gateway_need(item):
            raise ValueError(
                f"invalid capability need {item!r}: use a namespaced name such as "
                "'aws:<account>:<action-or-role>', 'ssh:<host>', 'github:<owner>:<scope>', "
                "'token-broker:<scope>', 'sudo:nopasswd' or 'browser:signed-in'")
        if item not in out:
            out.append(item)
    return out


def satisfies(held: str, need: str) -> bool:
    """``held`` covers ``need``: equal; an ``admin`` leaf at depth >= 3 covers its scope
    (``aws:1:admin`` covers ``aws:1:x``, but ``ssh:admin`` is just a host named admin); or a more
    specific holding of a broader need (``aws:1:admin`` covers ``aws:1``)."""
    if held == need:
        return True
    h, n = held.split(":"), need.split(":")
    if len(h) >= 3 and h[-1] == COVERING_LEAF and len(n) >= len(h) and n[: len(h) - 1] == h[:-1]:
        return True
    return len(h) > len(n) and h[: len(n)] == n


# --- config / paths ------------------------------------------------------------------------

_config_cache: tuple[float, dict] = (0.0, {})


def _config() -> dict:
    """``kanban.capabilities`` (cached for CONFIG_TTL seconds: the dispatcher tick reads it)."""
    global _config_cache
    at, cached = _config_cache
    if time.monotonic() - at < CONFIG_TTL and at:
        return cached
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("kanban", {}) or {}
        caps = cfg.get("capabilities") or {}
        caps = caps if isinstance(caps, dict) else {}
    except Exception:
        caps = {}
    _config_cache = (time.monotonic(), caps)
    return caps


def _seconds(cfg: dict, key: str, default: int) -> int:
    try:
        return max(30, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


def stale_after(cfg: Optional[dict] = None) -> int:
    return _seconds(_config() if cfg is None else cfg, "stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS)


def registry_path() -> Path:
    from hermes_cli.kanban_db import kanban_home
    return kanban_home() / "kanban" / "capabilities.json"


def hub_db_path() -> Path:
    return Path(os.environ.get("HERMES_HUB_STATE") or Path.home() / ".local/state/hermes-hub") / "hub.db"


def self_gateway() -> Optional[str]:
    """This gateway's hermes-hub name (hermes-mail config, else the replica's meta)."""
    cfg = Path(os.environ.get("HERMES_MAIL_CONFIG") or Path.home() / ".config/hermes-mail/config.json")
    try:
        gw = json.loads(cfg.read_text(encoding="utf-8-sig")).get("gateway")
        if isinstance(gw, str) and gw:
            return gw
    except (OSError, ValueError, AttributeError):
        pass
    try:
        db = _ro(hub_db_path())
        try:
            row = db.execute("SELECT v FROM meta WHERE k='gateway'").fetchone()
            return row[0] if row and row[0] else None
        finally:
            db.close()
    except Exception:
        return None


def _ro(path: Path):
    import sqlite3
    if not path.exists():
        raise FileNotFoundError(str(path))
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


# --- probes --------------------------------------------------------------------------------

def _run(cmd, timeout: int = PROBE_TIMEOUT) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd if isinstance(cmd, list) else ["bash", "-lc", cmd],
                           capture_output=True, text=True, timeout=timeout, check=False,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, str(exc)


def _builtin_github() -> list[tuple[str, bool]]:
    rc, out = _run(["gh", "api", "user", "--jq", ".login"])
    login = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", login or ""):
        return []
    found = [(f"github:{login}", True)]
    rc, status = _run(["gh", "auth", "status", "--active"])
    if rc != 0:
        rc, status = _run(["gh", "auth", "status"])
    m = re.search(r"Token scopes:\s*(.+)", status or "") if rc == 0 else None
    for scope in re.findall(r"'([a-z:_]+)'", m.group(1) if m else ""):
        found.append((f"github:{login}:{scope.replace(':', '-')}", True))
    return found


def probe_specs(cfg: dict) -> list[tuple[str, object]]:
    """(name, command) pairs from config, plus ssh hosts."""
    specs: list[tuple[str, object]] = []
    probes = cfg.get("probes") or {}
    if isinstance(probes, dict):
        for name, cmd in probes.items():
            if is_gateway_need(name) and isinstance(cmd, str) and cmd.strip():
                specs.append((name, cmd))
    hosts = cfg.get("ssh_hosts") or {}
    if isinstance(hosts, dict):
        for name, host in hosts.items():
            if isinstance(host, str) and host.strip() and is_gateway_need(f"ssh:{name}"):
                specs.append((f"ssh:{name}", ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                              host.strip(), "true"]))
    return specs


def refresh(cfg: Optional[dict] = None, *, now: Optional[float] = None) -> dict:
    """Run every probe now and atomically rewrite the registry file. Returns the record."""
    cfg = _config() if cfg is None else cfg
    specs = probe_specs(cfg)
    results: dict[str, dict] = {}

    def one(spec):
        name, cmd = spec
        rc, _ = _run(cmd)
        return name, rc == 0, time.time()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, s) for s in specs]
        if cfg.get("builtin", True):
            futures.append(pool.submit(lambda: ("sudo:nopasswd", _run(["sudo", "-k", "-n", "true"])[0] == 0, time.time())))
            gh = pool.submit(_builtin_github)
        else:
            gh = None
        for fut in futures:
            name, ok, at = fut.result()
            results[name] = {"name": name, "ok": ok, "checked_at": round(at, 3)}
        if gh is not None:
            at = round(time.time(), 3)
            for name, ok in gh.result():
                results.setdefault(name, {"name": name, "ok": ok, "checked_at": at})
    record = {
        "schema": SCHEMA,
        "gateway": self_gateway(),
        "refreshed_at": round(time.time() if now is None else now, 3),
        "refresh_seconds": _seconds(cfg, "refresh_seconds", DEFAULT_REFRESH_SECONDS),
        "stale_after_seconds": stale_after(cfg),
        "capabilities": sorted(results.values(), key=lambda r: r["name"]),
    }
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return record


def read_local() -> Optional[dict]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    except (OSError, ValueError):
        return None


def maybe_refresh_async(*, now: Optional[float] = None) -> bool:
    """Start a background refresh when the file is older than ``refresh_seconds``. Never waits,
    never raises: the dispatcher tick calls this."""
    global _thread
    try:
        if os.environ.get("HERMES_TEST_ISOLATION"):
            return False  # never run real probes (gh, sudo, ssh) from the test suite
        cfg = _config()
        if cfg.get("enabled") is False:
            return False
        now = time.time() if now is None else now
        local = read_local()
        if local and now - float(local.get("refreshed_at") or 0) < _seconds(cfg, "refresh_seconds",
                                                                              DEFAULT_REFRESH_SECONDS):
            return False
        with _lock:
            if _thread is not None and _thread.is_alive():
                return False
            _thread = threading.Thread(target=_safe_refresh, args=(cfg,), name="kanban-capabilities",
                                       daemon=True)
            _thread.start()
        return True
    except Exception:
        return False


def _safe_refresh(cfg):
    try:
        refresh(cfg)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("kanban capability refresh failed", exc_info=True)


# --- lookup --------------------------------------------------------------------------------

def _entries(record) -> list[dict]:
    caps = record.get("capabilities") if isinstance(record, dict) else None
    return [c for c in caps or [] if isinstance(c, dict) and isinstance(c.get("name"), str)]


def _match(record, need: str, now: float, limit: int) -> tuple[Optional[float], Optional[float]]:
    """(freshest fresh checked_at, freshest stale checked_at) of ok entries covering ``need``."""
    fresh = stale = None
    for c in _entries(record):
        if not c.get("ok") or not satisfies(c["name"], need):
            continue
        raw = c.get("checked_at")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        at = float(raw)
        if now - at <= limit:
            fresh = max(fresh or 0, at)
        else:
            stale = max(stale or 0, at)
    return fresh, stale


def remote_records(*, exclude: Optional[str] = None) -> dict[str, dict]:
    """``{gateway: capabilities record}`` from the hermes-hub replica (empty if unreadable)."""
    try:
        db = _ro(hub_db_path())
    except Exception:
        return {}
    try:
        rows = db.execute("SELECT gw, data FROM replica WHERE coll = 'capabilities' AND key = 'info'").fetchall()
    except Exception:
        rows = []
    finally:
        db.close()
    out = {}
    for gw, data in rows:
        if gw == exclude:
            continue
        try:
            rec = json.loads(data)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("schema") == SCHEMA:
            out[gw] = rec
    return out


def locate(need: str, *, now: Optional[float] = None, cfg: Optional[dict] = None,
           local: Optional[dict] = None, remote: Optional[dict] = None,
           self_gw: Optional[str] = None) -> dict:
    """Where ``need`` is held: ``{need, local: fresh|stale|absent, holders: [(gw, at)…] fresh
    newest first, stale: [(gw, at)…]}``. Only fresh entries are routable."""
    now = time.time() if now is None else now
    limit = stale_after(cfg)
    local = read_local() if local is None else local
    self_gw = self_gateway() if self_gw is None else self_gw
    remote = remote_records(exclude=self_gw) if remote is None else remote
    lf, ls = _match(local or {}, need, now, limit)
    holders, stale = [], []
    for gw, rec in sorted(remote.items()):
        if gw == self_gw:
            continue
        f, s = _match(rec, need, now, limit)
        if f is not None:
            holders.append((gw, f))
        elif s is not None:
            stale.append((gw, s))
    holders.sort(key=lambda x: -x[1])
    return {"need": need, "local": "fresh" if lf is not None else ("stale" if ls is not None else "absent"),
            "holders": holders, "stale": stale}


# --- CLI -----------------------------------------------------------------------------------

def run_cli(args) -> int:
    action = getattr(args, "cap_action", None) or "list"
    if action == "refresh":
        rec = refresh()
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0
    if action == "locate":
        try:
            needs = normalize_needs(args.needs)
        except ValueError as exc:
            print(str(exc))
            return 2
        out = [locate(n) for n in needs]
        print(json.dumps(out, indent=2))
        return 0 if all(o["local"] == "fresh" or o["holders"] for o in out) else 1
    local = read_local()
    gws = {self_gateway() or "local": local or {}}
    gws.update(remote_records(exclude=self_gateway()))
    now = time.time()
    limit = stale_after()
    for gw, rec in gws.items():
        for c in _entries(rec):
            age = now - float(c.get("checked_at") or 0)
            state = ("held" if c.get("ok") else "not held") + (" (stale)" if age > limit else "")
            print(f"{gw:10} {c['name']:48} {state:18} checked {int(age)}s ago")
    return 0


def describe_probe(cmd) -> str:
    return shlex.join(cmd) if isinstance(cmd, list) else str(cmd)
