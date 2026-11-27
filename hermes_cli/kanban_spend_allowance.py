"""Portable consumer of the Board's reviewer lease allowance (``board_lease_allowance_v1``). [daveey]

A canonical ``spend_review`` release writes the card's ``spend_budget_set`` with ``runs: 10``,
``runs_basis: board_lease_allowance_v1`` and the ``review_uid`` of the request it answers. The Board keeps a
private ledger for that exact release and charges every fresh lease grant against it (no refunds, no reset);
its existing signed ``spend_windows`` stay an independent intersection. Core therefore never counts the
allowance from replica ``task_runs`` history, on any host:

* :func:`state` classifies the card's newest override: ``None`` (no basis: ordinary cumulative accounting),
  :data:`BOARD` (a well-formed recognized override on a ``primary`` synced board: the run limit is the Board's,
  enforced on each grant) or :data:`UNPROVEN` (any other basis, a retired draft basis, malformed fields, or a
  board that is not ``primary``: the run limit holds). There is no cumulative or local fallback.
* :func:`grant_refusal` runs inside the claim transaction: a :data:`BOARD` card is admitted only when the
  actual lease grant returned evidence naming exactly this basis, review and budget event, with a charged
  non-negative ``remaining`` (a new Core against an old Board that sends none refuses).

The cache-read limit and every other gate keep their cumulative semantics; this module only replaces the run
count. Decision liveness lives beside it: ``kanban_allowance_observation`` reads the Board's allowance (bounded,
authenticated, never under a write transaction; a diagnostic, never debit proof) and
``kanban_allowance_maintenance`` moves an exhausted or decided-but-stuck card on its home into decision triage
with one notice. Only a human ``hermes kanban release`` supersedes an active allowance, through the Board's
canonical human ``renew_scope``; generic budget rows never do.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

BASIS = "board_lease_allowance_v1"
BOARD_RUNS = 10  # the canonical spend_review release: a fixed allowance of ten fresh leases
BOARD, UNPROVEN = "board", "unproven"
REFUSAL = "spend_allowance_unproven"


def _exact(value: Any, want: int) -> bool:
    return type(value) is int and value == want  # never a bool, a float (10.0) or a numeric string


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _newest(conn: sqlite3.Connection, task_id: str) -> Optional[tuple]:
    from hermes_cli import kanban_spend_budget as budget
    has_uid = "uid" in {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
    return conn.execute(f"SELECT {'uid' if has_uid else 'NULL'}, payload FROM task_events WHERE task_id = ? "
                        "AND kind = ? ORDER BY id DESC LIMIT 1", (task_id, budget.OVERRIDE_EVENT)).fetchone()


def expected(conn: sqlite3.Connection, task_id: str, data: Optional[dict] = None) -> Optional[dict]:
    """The exact grant evidence a :data:`BOARD` card needs, else None."""
    from hermes_cli import kanban_spend_budget as budget
    data = budget.card_override(conn, task_id) if data is None else data
    if not isinstance(data, dict) or data.get("runs_basis") != BASIS:
        return None
    row = _newest(conn, task_id)
    uid = row[0] if row else None
    if not (_exact(data.get("runs"), BOARD_RUNS) and _text(data.get("review_uid")) and _text(uid)):
        return None
    from hermes_cli import kanban_db_sync as sync
    if sync.board_mode(conn) != "primary":
        return None  # never a local allowance: only the Board's lease grant can enforce it
    return {"basis": BASIS, "review_uid": data["review_uid"], "budget_event_uid": uid}


def state(conn: sqlite3.Connection, task_id: str, data: Optional[dict] = None) -> Optional[str]:
    """``None`` (cumulative accounting), :data:`BOARD` or :data:`UNPROVEN` (the run limit holds)."""
    from hermes_cli import kanban_spend_budget as budget
    data = budget.card_override(conn, task_id) if data is None else data
    if not isinstance(data, dict) or "runs_basis" not in data:
        return None
    return BOARD if expected(conn, task_id, data) is not None else UNPROVEN


def history(conn: sqlite3.Connection, task_id: str) -> bool:
    """Some budget of this primary synced card named the Board basis, so the Board may still hold a private
    allowance for it whatever the newest local budget says. A reason to ask the Board, never a count."""
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli import kanban_db_sync as sync
    if sync.board_mode(conn) != "primary" or not sync.sync_state(conn):
        return False
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND json_valid(payload) "
        "AND json_extract(payload, '$.runs_basis') = ? LIMIT 1", (task_id, budget.OVERRIDE_EVENT, BASIS)).fetchone() is not None


def evidence_ok(want: Optional[dict], got: Any) -> bool:
    if want is None or not isinstance(got, dict):
        return False
    remaining = got.get("remaining")
    return (all(got.get(k) == v for k, v in want.items()) and got.get("charged") is True
            and type(remaining) is int and 0 <= remaining < BOARD_RUNS)


def claim_refused(conn: sqlite3.Connection, task_id: str, gate) -> bool:
    """Final transactional admission predicate shared by both claim lanes."""
    from hermes_cli import kanban_db_sync as sync
    refusal = grant_refusal(conn, task_id, gate)
    if refusal is None:
        return False
    sync.defer_claim(task_id, refusal, sync.ORIGIN_BOARD)
    return True  # the unused grant is released on gate exit


def grant_refusal(conn: sqlite3.Connection, task_id: str, gate) -> Optional[str]:
    """Inside the claim transaction: :data:`REFUSAL` when the card's run limit is the Board allowance and the
    gate's actual lease grant did not prove the exact charge (or there was no grant at all)."""
    current = state(conn, task_id)
    lease = getattr(gate, "lease", None)
    if current is None:
        # A paged/removed local budget cannot turn an actual portable debit into legacy authority.
        return REFUSAL if getattr(lease, "spend_allowance", None) is not None else None
    # ``unproven`` has no expected identity and no lease has no evidence: evidence_ok refuses both.
    return None if evidence_ok(expected(conn, task_id), getattr(lease, "spend_allowance", None)) else REFUSAL
