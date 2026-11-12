"""Recover a relay-minted OAuth row whose access token the minting host just revoked. [daveey]

The hub refreshes its Anthropic grants on a timer, and each refresh revokes the previous access
token at once. Worker nodes (and the mini's BB pool) hold those tokens as ``manual:hub_minted``
rows with no refresh token, copied by a pull job (``worker-oauth-pull``, every 30 s). A request
that lands between the hub's refresh and that pull gets ``401 OAuth access token has been
revoked``. The hub re-mints every account in one push, so rotating to the next row only hits the
next revoked token, and the whole pool is benched in under a second: the turn failed with
``auth`` and a Kanban worker exited 78 (terminal_provider), a manual unblock per card
(t_01a1d064a8dc8bd6: worker-4 run 10089, 15:30:35Z 401, hub push 15:30:37Z, pull 15:30:38Z).

No refresh can heal such a row; only a newer token on disk can. So on that 401 the agent runs
the pull itself (when this host has one), re-reads the pool and adopts the pushed token, polling
for up to ``oauth_relay.repull_wait_s`` (default 120 s) while the hub's push lands.
A host with no pull command keeps the old path (re-read, rotate, ``auth_wall_is_transient``).
Once per row per auth episode: a re-pulled token that is refused again is a genuine auth failure
and takes the old path (rotate, then fail). Rows with a refresh token, borrowed rows and other
401s never come here.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

MINTED_SOURCES = frozenset({"manual:hub_minted"})
DEFAULT_WAIT_S = 120.0
_PULL_EVERY_S = 10.0
_PULL_TIMEOUT_S = 60.0
_POLL_S = 1.0
_ACTIVITY_TOUCH_S = 30.0


def _ra():
    import run_agent
    return run_agent


def _settings() -> dict:
    """``oauth_relay`` in config.yaml: ``pull_command`` (an argv string) and ``repull_wait_s``."""
    try:
        from hermes_cli.config import load_config_readonly
        section = load_config_readonly().get("oauth_relay")
    except Exception:  # health: allow BLE001 -- an unreadable config keeps the defaults; the 401 path must not raise
        return {}
    return section if isinstance(section, dict) else {}


def is_relay_managed(entry: Any) -> bool:
    """An OAuth row a relay pushes (no refresh token of its own, not a borrowed singleton)."""
    from agent.credential_pool import AUTH_TYPE_OAUTH
    return (
        entry is not None
        and getattr(entry, "auth_type", None) == AUTH_TYPE_OAUTH
        and not (getattr(entry, "refresh_token", None) or "").strip()
        and (getattr(entry, "source", None) or "") in MINTED_SOURCES
    )


def is_revoked_401(status_code: Optional[int], error_context: Optional[dict]) -> bool:
    """A 401 whose body says the access token was revoked."""
    if status_code != 401 or not isinstance(error_context, dict):
        return False
    text = " ".join(str(error_context.get(k) or "") for k in ("message", "reason", "code")).lower()
    return "revoked" in text


MAC_PULL_JOB = "com.daveey.hub-oauth-pull"


def pull_command() -> Optional[list[str]]:
    """The host's relay pull: ``oauth_relay.pull_command``, else the host's own pull job.

    A worker node runs ``~/bin/worker-oauth-pull``; a Mac (MacBook, mini) runs the launchd job
    ``com.daveey.hub-oauth-pull`` every 120 s, so it is kickstarted, which keeps the job's own
    provider/skip environment. Both write the default home's ``auth.json`` only, so they are used
    only for that home; any other home (the mini's ``~/.bb-hermes``) needs ``pull_command``.
    """
    configured = str(_settings().get("pull_command") or "").strip()
    if configured:
        return shlex.split(configured)
    from hermes_constants import get_hermes_home
    home = Path.home()  # health: allow HX001 -- the host pull jobs and their target live in the user's home
    try:
        default_home = Path(get_hermes_home()).resolve() == (home / ".hermes").resolve()
    except OSError:
        return None
    if not default_home:
        return None
    script = home / "bin" / "worker-oauth-pull"
    if script.is_file() and os.access(script, os.X_OK):
        return [str(script)]
    if sys.platform == "darwin" and (home / "Library" / "LaunchAgents" / f"{MAC_PULL_JOB}.plist").is_file():
        return ["launchctl", "kickstart", f"gui/{os.getuid()}/{MAC_PULL_JOB}"]  # windows-footgun: ok -- darwin-gated
    return None


def _wait_budget() -> float:
    try:
        return max(0.0, float(_settings().get("repull_wait_s", DEFAULT_WAIT_S)))
    except (TypeError, ValueError):
        return DEFAULT_WAIT_S


def _run_pull(cmd: list[str]) -> None:
    """Run the pull once; leave a peer holding the lock to finish on its own.

    The command must bootstrap its own Python dependencies, not borrow the agent's
    PYTHONPATH/PYTHONHOME. All other relay settings remain inherited.
    """
    from hermes_constants import get_hermes_home
    lock_path = Path(get_hermes_home()) / "state" / "oauth-relay-repull.lock"
    log = _ra().logger
    try:
        import fcntl
    except ImportError:  # Windows has no relay pull host; run unlocked
        fcntl = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a", encoding="utf-8") as lock:
            if fcntl is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
            # The relay owns its interpreter (workers use /usr/bin/python3 for boto3).
            # PM's backend PYTHONPATH mixes its cryptography with distro OpenSSL and
            # fails at GEN_EMAIL. Preserve relay/AWS/home settings, not Python imports.
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=_PULL_TIMEOUT_S, stdin=subprocess.DEVNULL, check=False, env=env)
            if proc.returncode != 0:
                log.warning("OAuth relay re-pull %s exited %s: %s", cmd[0], proc.returncode,
                            (proc.stderr or proc.stdout or "").strip()[-300:])
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("OAuth relay re-pull %s failed: %s", cmd[0], exc)


def _find(pool: Any, entry_id: Optional[str], failed_key: Optional[str]) -> Any:
    entries = pool.entries()
    found = next((e for e in entries if entry_id and e.id == entry_id), None)
    return found or next((e for e in entries if failed_key and e.runtime_api_key == failed_key), None)


def await_pushed_token(agent: Any, pool: Any, *, entry_id: Optional[str], failed_key: Optional[str],
                       cmd: list[str]) -> Any:
    """Re-pull and poll until the failing row holds a new token; the adopted entry, or None.

    Every other relay row the push replaced is adopted too, so a rotation after this never
    lands on a token the same push already revoked.
    """
    log = _ra().logger
    start = time.monotonic()
    deadline = start + _wait_budget()
    next_pull = start
    next_touch = start + _ACTIVITY_TOUCH_S
    while True:
        if time.monotonic() >= next_pull:
            _run_pull(cmd)
            next_pull = time.monotonic() + _PULL_EVERY_S
        adopted = None
        for entry in list(pool.entries()):
            if not is_relay_managed(entry):
                continue
            fresh = pool._adopt_pushed_norefresh_row(entry)
            if fresh is not entry and fresh.id == entry_id:
                adopted = fresh
        if adopted is None:
            current = _find(pool, entry_id, failed_key)
            if current is not None and failed_key and current.runtime_api_key not in (None, "", failed_key):
                adopted = current  # a peer in this process already adopted the pushed token
        if adopted is not None:
            log.info("OAuth relay: adopted the pushed token for pool entry %s after %.1fs",
                     adopted.id, time.monotonic() - start)
            return adopted
        if time.monotonic() >= deadline or getattr(agent, "_interrupt_requested", False):
            log.warning("OAuth relay: no new token for pool entry %s within %.0fs of a revoked 401",
                        entry_id, time.monotonic() - start)
            return None
        time.sleep(_POLL_S)
        if time.monotonic() >= next_touch:
            touch = getattr(agent, "_touch_activity", None)
            if callable(touch):
                touch("waiting for the OAuth relay to push a re-minted token")
            next_touch = time.monotonic() + _ACTIVITY_TOUCH_S


def recover_revoked_relay_row(agent: Any, pool: Any, *, status_code: Optional[int],
                              error_context: Optional[dict], api_key_hint: Optional[str],
                              credential_id: Optional[str], episode: Any) -> Optional[bool]:
    """``True``/``False`` when this path owned the 401 (swap result), ``None`` to fall through.

    *episode* is ``credential_auth_recovery._episode`` (per-turn, per-entry state).
    """
    if not is_revoked_401(status_code, error_context):
        return None
    entry = _find(pool, credential_id, api_key_hint)
    if not is_relay_managed(entry):
        return None
    cmd = pull_command()
    if cmd is None:
        return None  # nothing to pull with here: the existing re-read/rotation path decides
    state = episode(agent, f"relay:{getattr(pool, 'provider', '')}", entry.id)
    if state.get("repulls"):
        # The re-pulled token was refused too: a genuine auth failure, not a pull lag.
        return None
    state["repulls"] = 1
    _ra().logger.info("OAuth relay: pool entry %s token revoked by a re-mint; re-pulling", entry.id)
    adopted = await_pushed_token(agent, pool, entry_id=entry.id, failed_key=api_key_hint, cmd=cmd)
    if adopted is None:
        return None
    pool._current_id = adopted.id
    return agent._swap_credential(adopted) is not False
