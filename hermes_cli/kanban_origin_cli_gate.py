"""Parent-owned admission/retirement ordering for a foreground CLI launch.

Retirement prevents NEW admissions, not completion of already admitted writes.
No IO or user callbacks run under this lock. An admitted request with no verified
successful reply has unknown outcome, including exceptions after COMMIT.
"""
from threading import Lock

from hermes_cli.kanban_origin_admission import AdmissionRefused


class LaunchGate:
    def __init__(self):
        self._lock = Lock()
        self._accepting = True
        self._requests = []

    def dispatch(self):
        with self._lock:
            if not self._accepting:
                raise AdmissionRefused("CLI launch retired before dispatch")
            request = RequestGate(self)
            self._requests.append(request)
            return request

    def retire(self):
        """Bounded state transition; never joins a handler or promises cancellation."""
        with self._lock:
            self._accepting = False
            return [request._report() for request in self._requests]


class RequestGate:
    def __init__(self, launch):
        self._launch = launch
        self._admitted = None
        self._finished = False
        self._verified = False

    def admit(self, task_id):
        with self._launch._lock:
            if not self._launch._accepting or self._admitted is not None or self._finished:
                raise AdmissionRefused("CLI launch retired or request already admitted/finished")
            self._admitted = task_id

    def finish(self, result=None):
        """A failed/absent reply cannot undo an admission or establish rollback."""
        with self._launch._lock:
            if self._finished:
                raise AdmissionRefused("CLI request already finished")
            self._finished = True
            self._verified = (self._admitted is not None and isinstance(result, dict)
                              and result.get("ok") is True and result.get("task_id") == self._admitted)
            return self._report()

    def _report(self):
        # Called only under the launch lock.
        if self._verified:
            outcome = "created"
        elif self._admitted is not None:
            outcome = "outcome_unknown"
        else:
            outcome = "no_board_commit"
        return {"outcome": outcome, "task_id": self._admitted, "finished": self._finished}
