"""Process-local custody of an ended turn whose durable release failed."""
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FailedTurnRelease:
    pid: int
    store: Path
    session_id: str
    holder: str


class FailedTurnReleasePending(RuntimeError):
    """A proven ended self-holder remains because its store still refuses release."""

    def __init__(self, receipt: FailedTurnRelease):
        super().__init__('Ended turn lease release is still unavailable')
        self.receipt = receipt


def remember_failed_release(agent, db, session_id: str, holder: str) -> None:
    """Only the lease's terminal release creates this receipt, never a PID observation."""
    path = getattr(db, 'db_path', None)
    if isinstance(path, (str, Path)):
        agent._failed_turn_release = FailedTurnRelease(os.getpid(), Path(path).resolve(), session_id, holder)


def settle_failed_release(agent, db, owner: dict) -> bool:
    """Retry an exact ended holder's conditional DELETE; never exempt it from DB admission.

    Called under the agent admission lock. Failure propagates: a readable owner row is not
    evidence that the store can release it. A successor between read and DELETE is protected
    by SessionDB's holder comparison, and the caller must re-read ownership afterwards.
    """
    receipt = getattr(agent, '_failed_turn_release', None)
    path = getattr(db, 'db_path', None)
    if (not isinstance(receipt, FailedTurnRelease) or receipt.pid != os.getpid()
            or not isinstance(path, (str, Path)) or Path(path).resolve() != receipt.store
            or owner['holder'] != receipt.holder):
        return False
    try:
        db.release_session_turn_lease(receipt.session_id, receipt.holder)
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        raise FailedTurnReleasePending(receipt) from exc
    agent._failed_turn_release = None
    return True
