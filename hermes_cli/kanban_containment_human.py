"""Human operator archive of a contained card: the third closure, ``human_operator``. [daveey]

A contained card closes only when its frozen authority is archived, or done with no producer. When the authority
can never run (``todo`` behind open parents, or itself in triage) nothing closes it: t_01a1fd2c65f894e3 was held
from 11-22 behind t_01a1fd3526eb2be9 although David had decided to archive it as superseded.

``hermes kanban archive <id> --reason "<why>"`` lets a human operator close every active episode instead. The caller
is classified exactly as ``renew-scope`` classifies a human (:func:`kanban_comment_writer.cli_comment_writer` ->
``human`` after the whole-ancestry walk); any agent, runner or delegate keeps the ordinary refusal. The archive is
refused while an authority has a live run or lease (it may still release the card) or while a safety restriction
holds the card. The authority card itself is left exactly as it is. The Board (``archiveContained``) enforces the
same rules on synced boards and advertises ``containment_human_archive: 1`` next to ``scope_renewal``;
``kanban_containment_terminal.archive_local`` enforces them on boards that never synced.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

CLOSURE = "human_operator"
REASON_MAX = 2000
AUTHOR = "kanban-operator"
CAPABILITY = "containment_human_archive"
KEYS = frozenset({"version", "episode", "containment_sha256", "reason", "authority", "authority_status",
                  "terminal_status", "actor_kind", "actor_gateway", "operator_reason"})


@dataclass(frozen=True)
class HumanArchive:
    """A classified human operator's archive request. Build it only with :func:`for_caller`."""
    reason: str


def _refused(message: str):
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    return ContainmentRefused(message)


def hint(task: str) -> str:
    return (f"a human operator can archive it from a terminal with `hermes kanban archive {task} --reason \"<why>\"` "
            "(refused while its authority has a live run or lease, or a safety restriction holds it)")


def valid_reason(reason: Any) -> Optional[str]:
    text = reason.strip() if isinstance(reason, str) else ""
    return text if text and len(text) <= REASON_MAX else None


def for_caller(reason: Any, *, writer: Optional[tuple] = None, env=None) -> HumanArchive:
    """The request for this CLI process, or ContainmentRefused. ``writer``/``env`` are test seams."""
    from hermes_cli.kanban_comment_writer import cli_comment_writer
    text = valid_reason(reason)
    if text is None:
        raise _refused(f"--reason must name why, in 1 to {REASON_MAX} characters")
    kind, _session, evidence = writer or cli_comment_writer(env=env)
    if kind != "human":
        raise _refused("only a human operator may archive a contained card with --reason; this caller was classified "
                       f"as {kind} ({evidence}). An agent needs the authority's release (resume-contained); ask a "
                       "human to run the archive from their own terminal")
    return HumanArchive(text)


def comment(human: HumanArchive) -> tuple[str, str]:
    return AUTHOR, f"Human operator archived this contained card (closure {CLOSURE}): {human.reason}"


def wire(human: HumanArchive, episode: str) -> dict:
    """The Board ``archive`` op's ``human_operator`` carriage."""
    return {"version": 1, "actor_kind": "human", "episode": episode, "reason": human.reason}


def audit(human: HumanArchive, gateway: str) -> dict:
    """What the archived event records, after any other extras and before ``source_status``."""
    return {"closure": CLOSURE, "actor_kind": "human", "actor_gateway": gateway, "reason": human.reason}


def closure_payload(base: dict, human: HumanArchive, gateway: str) -> dict:
    return {**base, "reason": CLOSURE, "actor_kind": "human", "actor_gateway": gateway, "operator_reason": human.reason}


def valid_closure(data: dict) -> bool:
    """The human-only fields of a ``containment_closed`` payload whose reason is ``human_operator``."""
    return (data.keys() == KEYS and data["actor_kind"] == "human" and isinstance(data["actor_gateway"], str)
            and bool(data["actor_gateway"]) and valid_reason(data["operator_reason"]) == data["operator_reason"]
            and isinstance(data["authority_status"], str) and bool(data["authority_status"]))


def dependents(conn, task: str) -> list[tuple[str, str, str]]:
    """``[(id, status, title)]`` of the unfinished cards that list ``task`` as a parent. A closed containment never
    satisfies them, and the hold refuses unlinking them."""
    return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in conn.execute(
        "SELECT t.id, t.status, t.title FROM task_links l JOIN tasks t ON t.id = l.child_id WHERE l.parent_id = ? "
        "AND t.status NOT IN ('done', 'archived') ORDER BY t.id", (task,))]


def safety_refusal(conn, task: str) -> Optional[str]:
    from hermes_cli.kanban_refusal_receipt import restricted
    if restricted(conn, task):
        return "safety_restricted: a safety restriction holds this card and a human containment archive does not lift it"
    return None


def authority_live(row) -> bool:
    """A local authority row that may still release the card: running, a current run, or an unexpired claim."""
    if row is None:
        return False
    keys = row.keys()
    expires = row["claim_expires"] if "claim_expires" in keys else None
    return (row["status"] == "running" or ("current_run_id" in keys and row["current_run_id"] is not None)
            or ("claim_lock" in keys and row["claim_lock"] is not None and (expires is None or expires > time.time())))
