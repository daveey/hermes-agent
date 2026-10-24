"""Internal judge-success capability; never accepted from worker metadata."""
from dataclasses import dataclass
import hashlib
import json


def subject(task, evidence):
    return {'task_id': task.id, 'title': task.title, 'body': task.body,
            'contract': getattr(task, 'completion_contract', None), 'run_id': getattr(task, 'current_run_id', None),
            'status': getattr(task, 'status', None), 'evidence': evidence.strip()}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class GoalVerification:
    snapshot: str

    @classmethod
    def for_handoff(cls, task, evidence):
        return cls(digest(subject(task, evidence)))


def record(conn, task_id, proof, evidence):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if (not task or not task.goal_mode or not isinstance(proof, GoalVerification)
            or proof.snapshot != digest(subject(task, evidence))):
        return None
    kb._append_event(conn, task_id, 'goal_verified',
                     {'subject': subject(task, evidence), 'snapshot': proof.snapshot},
                     run_id=task.current_run_id)
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]
