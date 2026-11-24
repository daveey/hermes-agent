"""Operator call ``label_publish``: a worker asks the hub to ADD skill:/capability: labels to one machine. [daveey]

``kanban_supervisor_request(ops=[{"op": "operator", "action": "label_publish", "machine": "worker-3",
"labels": ["capability:hub-read"], "proof": {"kind": "hub_probe"}}])``. It rides the operator call's request event
(``kanban_operator_call``), with three differences:

* **Executor.** The target is a machine, not a card, so the call's home is :data:`HOME`, the gateway that holds the
  Board admin key (``~/.config/hermes-mail/admin.key``). The requester never reads that key or runs an admin command;
  a home without the key refuses ``no_admin_key``.
* **Proof.** The requester names the proof kind; the hub does not trust any claim in it. For ``hub_probe`` the hub
  re-runs its own probe against that machine (:data:`PROBE_COMMAND <machine>`, the instance-role SSM read the
  skill-refresh timer uses) and publishes a label only when the probe proves it: ``skill:X`` must be in the
  machine's complete skill inventory, ``capability:X`` must have ``{"ok": true}`` in the probe's proofs. Anything
  unproven refuses the whole call ``bad_proof``. The probe prints one JSON object ``{"machine": <name>,
  "complete": true, "skills": ["skill:..."], "proofs": {"capability:...": {"ok": bool, "detail": str}}}``; a
  missing probe, a non-zero exit or unparseable output fails the call ``proof_unavailable`` and publishes nothing.
* **Result channel.** The hub writes ``operator_call_result`` on the REQUESTER's card through the Board (a single
  row op), so the requester's poll finds it there, vouched by the Board's ``origin_gateway``, and finishes the request
  as for the card actions.

The registry write is one atomic DELTA, never a whole-set replace: ``labels_add`` (hermes-hub 3ddfe728a8, client
1.3.12) makes the Board add the labels to the stored set inside its Durable Object, in the transaction that reads it,
so a concurrent writer's labels are never dropped and the client runs no compare-and-set loop. The delta only adds;
``machine:*`` and every other namespace are refused.

The probe and the admin calls take seconds to minutes, so they run in one background thread per process
(:func:`tick` starts it, a row in ``operator_label_jobs`` is its state, a stale row is retried); the dispatcher tick
only queues, starts and delivers. Every call is logged in ``operator_calls`` like the card actions.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

_log = logging.getLogger(__name__)

ACTION = "label_publish"
HOME = "hub"  # the Board gateway name of the hub (cards it creates carry created_on = "hub")
PROOF_KINDS = ("hub_probe",)
FIELDS = {"machine", "labels", "proof"}
NAMESPACES = ("skill:", "capability:")
LABEL_RE = re.compile(r"(skill|capability):[a-z0-9][a-z0-9._-]*")
LABEL_MAX_CHARS = 64  # the Board's LABEL_RE length
MACHINE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MAX_LABELS_PER_CALL = 16
BOARD_MAX_LABELS = 512  # the Board's MAX_LABELS (hermes-hub worker/src/mailbox.ts, 512 since hub #78), machine:<name> not counted
PROBE_TIMEOUT_S = 240
ADMIN_TIMEOUT_S = 45
ADMIN_CALLS_PER_JOB = 2  # one registry read, one labels_add
# Well above the worst job: the probe plus its admin calls (240 + 2 * 45 = 330 s).
JOB_STALE_S = 900
MAX_JOB_ATTEMPTS = 3
MAX_DELIVERY_ATTEMPTS = 20
DELIVERY_FREE_RETRIES = 3  # then back off 60 s doubling to 1 h: 20 attempts span ~13 h, inside the 24 h deadline
DELIVERY_BACKOFF_CAP_S = 3600
MAX_DELIVERIES_PER_TICK = 10
PROBE_COMMAND = "~/.local/share/hermes-worker-skills/bin/label-probe"  # hermes-state; absent = proof_unavailable
MAIL = "~/.local/share/hermes-mail/hermes_mail.py"
ADMIN_KEY = "~/.config/hermes-mail/admin.key"
LOCK_NAME = "operator-label-publish.lock"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS operator_label_jobs (
        request_uid TEXT PRIMARY KEY,
        board TEXT,
        machine TEXT NOT NULL,
        labels TEXT NOT NULL,
        caller TEXT,
        result_task_id TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        started_at INTEGER,
        outcome TEXT,
        detail TEXT,
        labels_after TEXT,
        event_uid TEXT,
        delivered_at INTEGER,
        delivery_attempts INTEGER NOT NULL DEFAULT 0,
        last_delivery_at INTEGER,
        last_error TEXT,
        supervisor_request_id INTEGER,
        created_at INTEGER NOT NULL)""",
)

_THREAD: dict = {}  # "t" -> the running job thread of this process


def _err(code: str, message: str):
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    return SupervisorRequestError(code, message)


# --- validation (both sides) ---------------------------------------------------------------------

def check_labels(raw: Any) -> list[str]:
    """The add-only delta, sorted and unique; ``ValueError`` names the first label it refuses."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("labels must be a non-empty list")
    if len(raw) > MAX_LABELS_PER_CALL:
        raise ValueError(f"at most {MAX_LABELS_PER_CALL} labels per call")
    out = set()
    for lab in raw:
        lab = str(lab or "").strip() if isinstance(lab, str) else ""
        if lab.startswith("machine:"):
            raise ValueError(f"{lab}: machine:* labels are never published (the Board sets a machine's own)")
        if not lab.startswith(NAMESPACES):
            raise ValueError(f"{lab or '<empty>'}: only skill:* and capability:* labels can be published")
        if len(lab) > LABEL_MAX_CHARS or not LABEL_RE.fullmatch(lab):
            raise ValueError(f"{lab}: not a valid label")
        out.add(lab)
    return sorted(out)


def check_machine(raw: Any) -> str:
    machine = str(raw or "").strip() if isinstance(raw, str) else ""
    if not MACHINE_RE.fullmatch(machine):
        raise ValueError("machine must be a Board gateway name")
    return machine


def check_proof(raw: Any) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in PROOF_KINDS or set(raw) - {"kind"}:
        raise ValueError(f"proof must be {{'kind': {' | '.join(repr(k) for k in PROOF_KINDS)}}}: the hub re-runs "
                         "its own probe against the machine; it takes no claimed result")
    return {"kind": raw["kind"]}


def normalize(i: int, op: dict) -> dict:
    extra = set(op) - {"op", "action"} - FIELDS
    if extra:
        raise _err("invalid_ops", f"op {i}: label_publish takes only machine, labels and proof (it names a machine, "
                   f"not a card); unexpected {', '.join(sorted(extra))}")
    try:
        machine = check_machine(op.get("machine"))
    except ValueError as exc:
        raise _err("invalid_ops", f"op {i}: {exc}") from None
    try:
        labels = check_labels(op.get("labels"))
    except ValueError as exc:
        raise _err("label_refused", f"op {i}: {exc}") from None
    try:
        proof = check_proof(op.get("proof"))
    except ValueError as exc:
        raise _err("bad_proof", f"op {i}: {exc}") from None
    return {"op": "operator", "action": ACTION, "machine": machine, "labels": labels, "proof": proof}


def preflight(conn: sqlite3.Connection, requester: str, op: dict, *, own: Optional[str], synced: bool,
              rate: Optional[str]) -> dict:
    verdict = {**op}
    if not synced or not own:
        return {**verdict, "verdict": "refused", "code": "not_synced",
                "reason": "this board does not sync to the Board, so the hub cannot be asked"}
    if rate:
        return {**verdict, "verdict": "refused", "code": "rate_limited", "reason": rate}
    return {**verdict, "verdict": "supervisor", "home": HOME}


# --- seams (tests replace these) -----------------------------------------------------------------

def _admin_key_present() -> bool:
    return Path(ADMIN_KEY).expanduser().is_file()


def _run(argv: list[str], timeout: float) -> str:
    from tools.environments.local import build_subprocess_env
    out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False,
                         stdin=subprocess.DEVNULL, env=build_subprocess_env())
    if out.returncode != 0:
        raise RuntimeError(f"{Path(argv[1] if len(argv) > 1 else argv[0]).name} exited {out.returncode}: "
                           f"{(out.stderr or out.stdout).strip()[-300:]}")
    return out.stdout


def _admin(*args: str) -> dict:
    """``hermes_mail.py admin <args>`` (the Board admin key; hub only)."""
    return json.loads(_run([sys.executable, str(Path(MAIL).expanduser()), "admin", *args], ADMIN_TIMEOUT_S))


def _probe(machine: str) -> dict:
    """The hub's own probe of ``machine``: ``{machine, complete, skills: [...], proofs: {label: {ok, detail}}}``."""
    cmd = Path(PROBE_COMMAND).expanduser()
    if not cmd.is_file():
        raise FileNotFoundError(f"no probe at {PROBE_COMMAND}")
    return json.loads(_run([sys.executable, str(cmd), machine], PROBE_TIMEOUT_S))


def _push_event(conn: sqlite3.Connection, board: str, own: str, task_id: str, event_uid: str,
                payload: dict) -> tuple[str, str]:
    """Write one ``operator_call_result`` event on ``task_id`` (another gateway's card) through the Board.

    Returns ``(status, detail)``; status is the Board's per-op answer (``applied``/``merged``/``rejected``)."""
    from hermes_cli.kanban_operator_label_delivery import push_event

    return push_event(conn, board, own, task_id, event_uid, payload)


def _start(fn: Callable[[], None]) -> Optional[threading.Thread]:
    from agent.memory_provider import spawn_context_thread
    t = spawn_context_thread(fn, name="kanban-label-publish")
    t.start()
    return t


@contextmanager
def _lock(conn: sqlite3.Connection) -> Iterator[None]:
    """Host-wide: one label job at a time across the gateway and CLI drains (the hub is Linux)."""
    import fcntl
    from hermes_cli import kanban_operator_call as kox
    path = Path(kox._db_path(conn) or ".").parent / LOCK_NAME
    with open(path, "ab") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- proof ---------------------------------------------------------------------------------------

def verify(machine: str, labels: list[str], probe: Any) -> Optional[str]:
    """None when the hub's probe proves every label, else why not."""
    if not isinstance(probe, dict) or probe.get("machine") != machine:
        return f"the probe did not answer for {machine}"
    skills = probe.get("skills") if probe.get("complete") is True else None
    skills = set(skills) if isinstance(skills, list) else None
    proofs = probe.get("proofs") if isinstance(probe.get("proofs"), dict) else {}
    unproven = []
    for lab in labels:
        if lab.startswith("skill:"):
            if skills is None or lab not in skills:
                unproven.append(f"{lab} (not in {machine}'s {'complete ' if skills is None else ''}skill inventory)")
        else:
            res = proofs.get(lab)
            res = res if isinstance(res, dict) else {}
            if res.get("ok") is not True:
                why = res.get("detail")
                unproven.append(f"{lab} ({'probe failed: ' + str(why)[:120] if why else 'no probe proves it'})")
    return "; ".join(unproven) if unproven else None


# --- atomic delta --------------------------------------------------------------------------------

def _own(row: Any, machine: str) -> Optional[set]:
    """A registry row's labels without the implicit ``machine:<name>``; None when the row is not this machine."""
    if not isinstance(row, dict) or row.get("name") != machine or not isinstance(row.get("labels"), list):
        return None
    return {x for x in row["labels"] if isinstance(x, str) and x != f"machine:{machine}"}


def _current(machine: str) -> Optional[set]:
    rows = [g for g in (_admin("machines").get("gateways") or []) if isinstance(g, dict)
            and g.get("name") == machine and g.get("state") == "active"]
    return _own(rows[0], machine) if rows else None


def publish(machine: str, delta: list[str]) -> tuple[str, str, Optional[list[str]]]:
    """Add ``delta`` to ``machine``'s registry labels: ``(outcome, detail, labels_after)``.

    One ``labels_add`` write (``hermes-mail admin machine <name> --add-labels``, client 1.3.12+). The Board applies
    the delta to the stored set inside its Durable Object, in the same storage transaction that reads it, so a
    concurrent writer's labels are never dropped and there is no client-side compare-and-set. The answer is the
    resulting set; the call is ``applied`` only when it holds the whole delta (a later write may change it again,
    which is that writer's decision, not a lost update). A read first skips the write (and its audit row) when every
    label is already there, and refuses an unknown or retired machine and a delta past the Board's label cap; the
    Board re-checks both inside the write."""
    want = set(delta)
    cur = _current(machine)
    if cur is None:
        return "refused", f"unknown_machine: no active gateway {machine} in the Board registry", None
    if want <= cur:
        return "already_applied", "", sorted(cur)
    if len(cur | want) > BOARD_MAX_LABELS:
        return "refused", f"too_many_labels: {len(cur | want)} exceed the Board cap of {BOARD_MAX_LABELS}", None
    after = _own(_admin("machine", machine, "--add-labels", ",".join(sorted(want))), machine)
    if after is None:
        return "failed", f"the Board's answer to the labels_add on {machine} carries no label set", None
    if not want <= after:
        return "failed", (f"the Board's answer to the labels_add on {machine} lacks {sorted(want - after)}: is the "
                          "Board older than hermes-hub 3ddfe728a8 (labels_add)?"), None
    return "applied", "", sorted(after)


def execute(machine: str, labels: list[str]) -> tuple[str, str, Optional[list[str]]]:
    """Hub side, in the job thread: prove, then publish."""
    if not _admin_key_present():
        return "refused", "no_admin_key: this gateway has no Board admin key, only the hub publishes labels", None
    try:
        probe = _probe(machine)
    except Exception as exc:  # health: allow BLE001 -- an unreadable probe is an unproven label, never a publish
        return "failed", f"proof_unavailable: {type(exc).__name__}: {str(exc)[:200]}", None
    why = verify(machine, labels, probe)
    if why:
        return "refused", f"bad_proof: the hub's probe of {machine} does not prove {why}", None
    try:
        return publish(machine, labels)
    except Exception as exc:  # health: allow BLE001 -- recorded as a failed call, never silent
        _log.exception("kanban label_publish on %s failed", machine)
        return "failed", f"{type(exc).__name__}: {str(exc)[:300]}", None


# --- home side: queue, run, deliver --------------------------------------------------------------

def handle(conn: sqlite3.Connection, own: str, event: dict, payload: dict, refusal: Optional[str]) -> str:
    """Queue one proven call (or record its refusal) on the hub; :func:`tick` runs and answers it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_operator_call as kox
    uid, caller = payload["request_uid"], payload.get("caller") or {}
    args = payload.get("args") or {}
    machine, labels = str(args.get("machine") or "")[:64], []
    if refusal is None:
        try:
            machine = check_machine(args.get("machine"))
            labels = check_labels(args.get("labels"))
            check_proof(args.get("proof"))
        except ValueError as exc:
            refusal = f"invalid: {exc}"
    if refusal is None and not _admin_key_present():
        refusal = "no_admin_key: this gateway has no Board admin key, only the hub publishes labels"
    outcome = "refused" if refusal else "queued"
    op = {"action": ACTION, "machine": machine, "labels": labels}
    now = int(time.time())
    with kb.write_txn(conn):
        kox.log(conn, request_uid=uid, side="execute", outcome=outcome, op=op, caller=caller, detail=refusal,
                board=payload.get("board"), executor_gateway=own,
                supervisor_request_id=payload.get("supervisor_request_id"), finished=bool(refusal))
        conn.execute(
            "INSERT OR IGNORE INTO operator_label_jobs (request_uid, board, machine, labels, caller, result_task_id, "
            "state, outcome, detail, event_uid, supervisor_request_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, payload.get("board"), machine, json.dumps(labels),
             json.dumps({k: caller.get(k) for k in ("gateway", "task_id", "run_id")}), event["task_id"],
             "done" if refusal else "queued", "refused" if refusal else None, refusal, str(uuid.uuid4()),
             payload.get("supervisor_request_id"), now))
    return outcome


def _finish_job(conn: sqlite3.Connection, uid: str, attempt: int, outcome: str, detail: str,
                after: Optional[list[str]]) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_operator_call as kox
    with kb.write_txn(conn):
        cur = conn.execute("UPDATE operator_label_jobs SET state='done', outcome=?, detail=?, labels_after=? "
                           "WHERE request_uid=? AND state='running' AND attempts=?",
                           (outcome, detail or None, json.dumps(after) if after is not None else None, uid, attempt))
        if cur.rowcount == 1:
            kox.log(conn, request_uid=uid, side="execute", outcome=outcome, detail=detail or None, finished=True)


def _run_job(db_path: str, uid: str, attempt: int) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    conn = connect(Path(db_path))
    try:
        with _lock(conn):
            row = conn.execute("SELECT machine, labels, state, attempts FROM operator_label_jobs WHERE request_uid=?",
                               (uid,)).fetchone()
            if row is None or row[2] != "running" or int(row[3]) != attempt:
                return
            with kb.write_txn(conn):  # the stale clock starts once the lock is ours, not at the claim
                conn.execute("UPDATE operator_label_jobs SET started_at=? WHERE request_uid=? AND attempts=?",
                             (int(time.time()), uid, attempt))
            outcome, detail, after = execute(row[0], json.loads(row[1]))
            _finish_job(conn, uid, attempt, outcome, detail, after)
            _log.info("kanban label_publish %s %s on %s: %s %s", uid, json.loads(row[1]), row[0], outcome, detail)
    except Exception:  # health: allow BLE001 -- the row stays running; the stale sweep retries it
        _log.exception("kanban label_publish job %s crashed", uid)
    finally:
        conn.close()


def _claim(conn: sqlite3.Connection, now: int) -> Optional[tuple[str, int]]:
    """Take the oldest queued (or stale running) job; a job that died :data:`MAX_JOB_ATTEMPTS` times fails."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_operator_call as kox
    pick = ("SELECT request_uid, attempts FROM operator_label_jobs WHERE state='queued' OR "
            "(state='running' AND started_at < ?) ORDER BY created_at LIMIT 1")
    if conn.execute(pick, (now - JOB_STALE_S,)).fetchone() is None:
        return None  # an idle hub costs no write per tick
    with kb.write_txn(conn):
        row = conn.execute(pick, (now - JOB_STALE_S,)).fetchone()
        if row is None:
            return None
        uid, attempts = row[0], int(row[1])
        if attempts >= MAX_JOB_ATTEMPTS:
            detail = f"the label job stopped {attempts} times without an answer"
            conn.execute("UPDATE operator_label_jobs SET state='done', outcome='failed', detail=? WHERE request_uid=?",
                         (detail, uid))
            kox.log(conn, request_uid=uid, side="execute", outcome="failed", detail=detail, finished=True)
            return None
        conn.execute("UPDATE operator_label_jobs SET state='running', attempts=?, started_at=? WHERE request_uid=?",
                     (attempts + 1, now, uid))
        return uid, attempts + 1


def _delivery_due(tries: int, last: Optional[int], now: int) -> bool:
    if tries < DELIVERY_FREE_RETRIES or last is None:
        return True
    return now - int(last) >= min(60 * 2 ** (tries - DELIVERY_FREE_RETRIES), DELIVERY_BACKOFF_CAP_S)


def _deliver(conn: sqlite3.Connection, own: str, board: str, now: Optional[int] = None) -> int:
    from hermes_cli import kanban_db as kb
    now = int(time.time()) if now is None else now
    rows = conn.execute("SELECT request_uid, board, machine, labels, caller, result_task_id, outcome, detail, "
                        "labels_after, event_uid, delivery_attempts, last_delivery_at FROM operator_label_jobs "
                        "WHERE state='done' AND delivered_at IS NULL ORDER BY created_at").fetchall()
    rows = [r for r in rows if _delivery_due(int(r[10] or 0), r[11], now)][:MAX_DELIVERIES_PER_TICK]
    sent = 0
    for (uid, jboard, machine, labels, caller, task, outcome, detail, after, euid, tries, _last) in rows:
        payload = {"request_uid": uid, "action": ACTION, "outcome": outcome, "detail": detail,
                   "executor_gateway": own, "machine": machine, "labels": json.loads(labels or "[]"),
                   "labels_after": json.loads(after) if after else None, "caller": json.loads(caller or "{}")}
        try:
            status, why = _push_event(conn, jboard or board, own, task, euid, payload)
        except Exception as exc:  # health: allow BLE001 -- unreachable Board: retried next tick
            status, why = "error", f"{type(exc).__name__}: {exc}"
        done = status in ("applied", "merged") or (status == "rejected" and why.startswith("not_found")) \
            or tries + 1 >= MAX_DELIVERY_ATTEMPTS
        with kb.write_txn(conn):
            conn.execute("UPDATE operator_label_jobs SET delivery_attempts=delivery_attempts+1, last_error=?, "
                         "delivered_at=?, last_delivery_at=? WHERE request_uid=?",
                         (None if status in ("applied", "merged") else why[:300], int(time.time()) if done else None,
                          now, uid))
        sent += status in ("applied", "merged")
    return sent


def tick(conn: sqlite3.Connection, own: str, board: str, *, now: Optional[int] = None) -> None:
    """Each home dispatcher tick: start the next job (one per process at a time), deliver finished answers."""
    from hermes_cli import kanban_operator_call as kox
    now = int(time.time()) if now is None else now
    alive = _THREAD.get("t")
    if not (alive is not None and alive.is_alive()):
        path = kox._db_path(conn)
        claimed = _claim(conn, now) if path else None
        if path and claimed is not None:
            uid, attempt = claimed
            _THREAD["t"] = _start(lambda: _run_job(path, uid, attempt))
    _deliver(conn, own, board, now)
