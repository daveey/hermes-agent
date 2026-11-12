"""Fail-closed systemd inventory only for the Linux opted-in pre-swap stop.

Fleet restart's best-effort helpers deliberately conflate unknown and excluded. That
contract must not authorize a checkout move; leave those shared helpers unchanged.
"""
from __future__ import annotations

import subprocess


def systemd_units() -> list[dict]:
    from hermes_cli import update_cmd_fleet as fleet
    from hermes_cli import update_cmd_posix_pause as pause
    from hermes_cli.update_fleet_scope import (
        announce_skipped_runtime, gateway_foreign_code_root, home_in_update_scope,
        systemd_unit_hermes_home,
    )

    def read(cmd, identity):
        try:
            result = fleet._systemctl(cmd, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise pause.PauseRefused(f"could not inventory {identity}: {exc}", [identity]) from exc
        if result.returncode != 0:
            raise pause.PauseRefused(f"could not inventory {identity}: {(result.stderr or '').strip()}", [identity])
        return (result.stdout or "").strip()

    units, seen = [], set()
    for scope, cmd in fleet._SYSTEMD_SCOPES:
        identity = f"systemd {scope} gateway inventory"
        listing = read(cmd + fleet._LIST_GATEWAY_UNITS, identity)
        names = []
        fleet._for_each_systemd_gateway_unit(listing, process_unit=names.append,
                                            on_unit_timeout=lambda name, exc: None)
        for name in names:
            if not name.startswith("hermes-gateway"):
                continue
            value = read(cmd + ["show", name, "--property=MainPID", "--value"], name)
            if not value.isdecimal():
                raise pause.PauseRefused(f"{name} has unreadable MainPID {value!r}", [name])
            pid = int(value)
            if pid == 0:
                state = read(cmd + ["show", name, "--property=ActiveState", "--value"], name)
                if state not in ("inactive", "failed"):
                    raise pause.PauseRefused(f"{name} has no MainPID but is not proven inactive ({state!r})", [name])
                continue
            home = systemd_unit_hermes_home(cmd, name)
            if home is None:
                raise pause.PauseRefused(f"{name} ownership could not be read", [name])
            if not home_in_update_scope(home):
                announce_skipped_runtime("systemd unit", name, home)
                continue
            pinned = gateway_foreign_code_root(pid, home)
            if pinned is not None:
                announce_skipped_runtime("systemd unit", name, home, code_root=pinned)
                continue
            if pid not in seen:
                seen.add(pid)
                units.append({"kind": "systemd", "scope": scope, "unit": name, "pid": pid,
                              "cgroup": pause._unit_cgroup(cmd, name), "home": home})
    return units
