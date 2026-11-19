"""Read local turn-urgency observations without contacting their sources."""

import json
import math
import re
from pathlib import Path


def _finite_clock(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def read_owners(*, board: str, now_s: float, path=None) -> set[str]:
    """Return cached owner IDs, or no urgency when observation is invalid."""
    from hermes_cli import kanban_fairness_runtime as runtime

    if not isinstance(board, str) or not runtime.enabled(board=board):
        return _read_owners(board=board, now_s=now_s, path=path, capture=False)
    with runtime.observation(board=board):
        return _read_owners(board=board, now_s=now_s, path=path, capture=True)


def _read_owners(*, board, now_s, path, capture):
    raw = None

    def result(owners=None, outcome="invalid"):
        owners = set() if owners is None else owners
        if capture:
            from hermes_cli.kanban_fairness_urgency import emit

            emit(board=board, now_s=now_s if _finite_clock(now_s) else None,
                 raw=raw, outcome=outcome, owners=owners)
        return owners

    if not _nonempty(board) or not _finite_clock(now_s):
        return result(outcome="invalid_request")
    if path is None:
        from hermes_constants import get_hermes_home

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", board):
            return result(outcome="invalid_request")
        filename = "kanban-turn-urgency.json" if board == "default" else f"kanban-turn-urgency-{board}.json"
        path = get_hermes_home() / "cache" / filename
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return result(outcome="oversize")
        value = json.loads(raw)
    except FileNotFoundError:
        return result(outcome="missing")
    except OSError:
        return result(outcome="io_error")
    except (ValueError, UnicodeError, RecursionError):
        return result()
    if not isinstance(value, dict):
        return result()
    observed = value.get("observed_at")
    valid_until = value.get("valid_until")
    if (
        type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("board") != board
        or not _nonempty(value.get("source_revision"))
        or not _finite_clock(observed)
        or not _finite_clock(valid_until)
        or not observed <= valid_until <= observed + 120
        or observed > now_s
        or not isinstance(value.get("owners"), list)
    ):
        return result()
    if valid_until <= now_s:
        return result(outcome="expired")
    if len(value["owners"]) > 4096:
        return result()
    owners = set()
    for owner in value["owners"]:
        if not isinstance(owner, dict):
            return result()
        if (not all(_nonempty(owner.get(key)) for key in ("task_id", "source", "ref", "head"))
                or owner["source"] not in ("core-lane", "state-turn")
                or not _finite_clock(owner.get("expires_at"))
                or owner["expires_at"] <= now_s):
            expired = _finite_clock(owner.get("expires_at")) and owner["expires_at"] <= now_s
            return result(outcome="expired" if expired else "invalid")
        owners.add(owner["task_id"])
    return result(owners, "valid")
