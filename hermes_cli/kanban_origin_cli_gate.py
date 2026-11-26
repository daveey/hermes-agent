"""Parent-owned admission/retirement ordering for a foreground CLI launch.

Retirement prevents NEW admissions, not completion of already admitted writes.
No IO or user callbacks run under this lock. An admitted request with no verified
successful reply has unknown outcome, including exceptions after COMMIT.
"""
from threading import Lock

from hermes_cli.kanban_origin_admission import AdmissionRefused

_RELEASE_FAILURES = ("expired", "refused")
_RELEASE_STAGES = (
    "intent_journaled_unsent", "authorized_pending_home", "read_first_pending", "acknowledged_pending_home", "consumed_or_applied", "applied",
)


class LaunchGate:
    def __init__(self):
        self._lock = Lock()
        self._accepting = True
        self._requests = []

    def dispatch(self, *, operation="create"):
        if operation not in ("create", "release"):
            raise ValueError("unknown native CLI operation")
        with self._lock:
            if not self._accepting:
                raise AdmissionRefused("CLI launch retired before dispatch")
            request = RequestGate(self, operation)
            self._requests.append(request)
            return request

    def retire(self):
        """Bounded state transition; never joins a handler or promises cancellation."""
        with self._lock:
            self._accepting = False
            return [request._report() for request in self._requests]


class RequestGate:
    def __init__(self, launch, operation="create"):
        self._launch = launch
        self._operation = operation
        self._intent_uid = None
        self._release_stage = None
        self._admitted = None
        self._finished = False
        self._verified = False

    def admit(self, task_id, *, intent_uid=None):
        with self._launch._lock:
            if not self._launch._accepting or self._admitted is not None or self._finished:
                raise AdmissionRefused("CLI launch retired or request already admitted/finished")
            if self._operation == "release" and (not isinstance(intent_uid, str) or not intent_uid):
                raise AdmissionRefused("release admission requires an exact intent")
            if self._operation == "create" and intent_uid is not None:
                raise AdmissionRefused("creation admission cannot journal a release intent")
            self._admitted = task_id
            self._intent_uid = intent_uid

    def finish(self, result=None):
        """A failed/absent reply cannot undo an admission or establish rollback."""
        with self._launch._lock:
            if self._finished:
                raise AdmissionRefused("CLI request already finished")
            self._finished = True
            if (self._admitted is not None and isinstance(result, dict)
                    and result.get("task_id") == self._admitted):
                if self._operation == "release":
                    stage = result.get("release_stage")
                    self._verified = (result.get("intent_uid") == self._intent_uid
                                      and ((result.get("ok") is True and stage in _RELEASE_STAGES)
                                           or (result.get("ok") is False and stage in _RELEASE_FAILURES)))
                    if self._verified:
                        self._release_stage = stage
                else:
                    self._verified = result.get("ok") is True
            return self._report()

    def _report(self):
        # Called only under the launch lock.
        if self._verified:
            outcome = self._release_stage if self._operation == "release" else "created"
        elif self._admitted is not None:
            outcome = "outcome_unknown"
        else:
            outcome = "no_board_commit"
        report = {"outcome": outcome, "task_id": self._admitted, "finished": self._finished}
        if self._operation == "release":
            report.update(operation="release", intent_uid=self._intent_uid)
        return report
