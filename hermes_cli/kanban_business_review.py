"""Host zero-tool business review adapter (CONTRACT199 revision 6).

One review operation per review task, driven by :func:`prepare`. The Board's
custody (``review_admit`` on a lease grant, settlement through ``/ops`` and the
read-only review-receipt route) is the authority; this module keeps a private
sidecar so a crash or a lost reply can resume without ever starting a second
review for the same identity. The reviewer is a separate process with no tool
inventory that calls a class selector, never a named model or provider.

Nothing calls this module yet: it is reachable only through ``-m`` and tests.
Import-time dependencies are the standard library only; ``hermes_cli``,
``agent`` and ``model_tools`` are imported inside functions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

try:  # flock is required; on a platform without it prepare raises.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

INSTRUCTION = (b"Review the bound diff. Reply with one JSON object, keys verdict, summary, findings. "
               b"verdict is approve or request_changes. No other keys.")
INSTRUCTION_DIGEST = hashlib.sha256(INSTRUCTION).hexdigest()
EMPTY_CAPABILITY_DIGEST = hashlib.sha256(b"[]").hexdigest()
POLICY_VERSION = "bb-zero-tool-v1"

ENVELOPE_KEYS = (
    "author_session", "base_sha", "capability_digest", "full_diff_sha256", "head_sha", "landing_task",
    "policy_version", "prompt_digest", "repair_target", "repo", "review_task", "reviewer_session",
)
METADATA_KEYS = (
    "author_session", "repo", "head_sha", "base_sha", "full_diff_sha256", "policy_version", "prompt_digest",
    "repair_target", "landing_task", "diff_path",
)
_HEX = {"head_sha": 40, "base_sha": 40, "full_diff_sha256": 64, "prompt_digest": 64}
MAX_FIELD = 256
MAX_DIFF = 1 << 20
MAX_OUTPUT = 64 * 1024
GRANT_MAX = 4
SETTLE_MAX = 2
LEASE_TTL_S = 900
SPAWN_MARGIN_MS = 120001
CHILD_ENV_DROP = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_ORIGIN_SESSION")
FORBIDDEN_KWARGS = ("tools", "functions", "tool_choice", "function_call", "parallel_tool_calls")
TERMINAL = ("approved", "repair", "closed")
NO_EFFECT_FLAGS = ("restart_permitted", "settlement_permitted", "resend_permitted")

COLUMNS = (
    "task_id", "source_run_id", "run_uid", "admission_uid", "operation_uid", "envelope_digest", "grant_op_id",
    "settle_op_id", "grant_json", "settle_json", *ENVELOPE_KEYS, "prompt_sha256", "implementer",
    "hard_deadline_ms", "local_state", "work_dir", "grant_attempts", "settle_attempts", "last_class",
    "last_body", "route_provider", "route_model", "updated_at_ms",
)


class ToolInventoryRefused(RuntimeError):
    """A reviewer request or response carried, or could carry, a tool."""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def envelope_digest(envelope: dict, admission_uid: str, operation_uid: str) -> str:
    return sha256_hex(canonical({**envelope, "admission_uid": admission_uid,
                                 "operation_uid": operation_uid, "protocol": 1}))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _kanban_home() -> Path:
    from hermes_cli.kanban_db import kanban_home
    return kanban_home()


def _quoted(board: str) -> str:
    return urllib.parse.quote(board, safe="")


def _base(board: str) -> Path:
    if not board or "/" in board or board.startswith(".") or "\x00" in board:
        raise ValueError(f"unsafe board name: {board!r}")
    return _kanban_home() / "business-review"


# --------------------------------------------------------------------- receipt

def _read(hub, board: str, identity: tuple) -> tuple[str, Any]:
    task, run_uid, admission_uid, operation_uid, env_digest = identity
    query = urllib.parse.urlencode({"task": task, "run": run_uid, "admission": admission_uid,
                                    "operation": operation_uid, "envelope": env_digest})
    path = f"/v2/boards/{_quoted(board)}/business-commands/review-receipt?{query}"
    try:
        body = hub._call("GET", path)
    except Exception as exc:  # health: allow BLE001 -- fail closed: any transport error classifies as refused or unavailable, never authority
        try:
            from hermes_cli.kanban_sync_client import HubError
        except Exception:  # pragma: no cover  # health: allow BLE001 -- core always ships it; absent means every error is unavailable
            HubError = ()  # type: ignore[assignment]
        if HubError and isinstance(exc, HubError) and getattr(exc, "status", None) in (400, 401, 403):
            return "refused", None
        return "unavailable", None
    return _classify(body, task, run_uid, operation_uid), body


def _classify(body: Any, task: str, run_uid: str, operation_uid: str) -> str:
    if not isinstance(body, dict) or any(body.get(flag) is not False for flag in NO_EFFECT_FLAGS):
        return "unavailable"
    if "receipt" not in body:
        return "unavailable"
    state, receipt = body.get("state"), body["receipt"]
    if state == "absent" and receipt is None:
        return "absent"
    if state == "admitted" and receipt is None:
        return "live"
    if isinstance(receipt, dict):
        same = (receipt.get("task") == task and receipt.get("run_uid") == run_uid
                and receipt.get("operation_uid") == operation_uid)
        verdict = receipt.get("verdict")
        if state == "applied" and same and verdict == "approved":
            return "approved"
        if state == "applied" and same and verdict == "changes_requested":
            return "repair"
        if state == "rejected" and same and verdict == "malformed":
            return "closed"
    if state == "sealed" and (receipt is None or (isinstance(receipt, dict) and receipt.get("verdict") == "no_verdict")):
        return "closed"
    return "unavailable"


def read_receipt(hub, board: str, identity: tuple) -> str:
    """Classify the Board's review receipt for one exact identity. Writes nothing."""
    return _read(hub, board, identity)[0]


def qualify(hub, board: str, identity: tuple) -> str:
    """``approved`` only on a fresh ``approved`` receipt; never reads the sidecar."""
    return "approved" if read_receipt(hub, board, identity) == "approved" else "not_approved"


# --------------------------------------------------------------------- handoff

def _str_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_FIELD


def _read_diff(diff_path: str) -> Optional[bytes]:
    try:
        raw = Path(diff_path)
        if raw.is_symlink():
            return None
        resolved = raw.resolve()
        home = _kanban_home().resolve()
        if not resolved.is_relative_to(home):
            return None
        fd = os.open(str(raw), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode) or st.st_size > MAX_DIFF:
                return None
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, MAX_DIFF + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_DIFF:
                    return None
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError:
        return None


def _handoff(conn: sqlite3.Connection, review_task: str, reviewer_session: str) -> Optional[dict]:
    """The bound envelope for ``review_task``, or ``None`` (unqualified). Reads only."""
    from hermes_cli.kanban_db import get_task
    task = get_task(conn, review_task)
    if task is None:
        return None
    event = conn.execute(
        "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1", (review_task,)).fetchone()
    if event is None or event[0] is None:
        return None
    try:
        payload = json.loads(event[1]) if event[1] else {}
    except (TypeError, ValueError):
        return None
    implementer = payload.get("implementer") if isinstance(payload, dict) else None
    if not isinstance(implementer, str) or not implementer.strip():
        return None
    run = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (event[0],)).fetchone()
    if run is None:
        return None
    try:
        meta = json.loads(run[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    for key in METADATA_KEYS:
        if not _str_ok(meta.get(key)):
            return None
    for key, width in _HEX.items():
        if not re.fullmatch(f"[0-9a-f]{{{width}}}", meta[key]):
            return None
    if meta["policy_version"] != POLICY_VERSION or meta["prompt_digest"] != INSTRUCTION_DIGEST:
        return None
    ids = (meta["repair_target"], review_task, meta["landing_task"])
    if len(set(ids)) != 3 or getattr(task, "status", None) != "review":
        return None
    for parent, child in ((meta["repair_target"], review_task), (review_task, meta["landing_task"])):
        if conn.execute("SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
                        (parent, child)).fetchone() is None:
            return None
    if reviewer_session == meta["author_session"]:
        return None
    diff = _read_diff(meta["diff_path"])
    if diff is None or sha256_hex(diff) != meta["full_diff_sha256"]:
        return None
    envelope = {key: meta[key] for key in ENVELOPE_KEYS if key in meta}
    envelope.update(capability_digest=EMPTY_CAPABILITY_DIGEST, review_task=review_task,
                    reviewer_session=reviewer_session)
    return {"envelope": envelope, "implementer": implementer, "source_run_id": event[0], "diff": diff}


# --------------------------------------------------------------------- sidecar

def _sidecar_path(board: str) -> Path:
    return _base(board) / f"{board}.db"


def _connect(path: Path) -> sqlite3.Connection:
    # Dedicated business-review operations journal, never a live Kanban board.
    side = sqlite3.connect(str(path), timeout=30, isolation_level=None)  # noqa: kanban-lockguard
    side.row_factory = sqlite3.Row
    return side


def _open_sidecar(board: str, create: bool) -> Optional[sqlite3.Connection]:
    path = _sidecar_path(board)
    if not path.exists():
        if not create:
            return None
        try:
            os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
    side = _connect(path)
    cols = ", ".join(f"{c} {'TEXT PRIMARY KEY' if c == 'task_id' else ''}" for c in COLUMNS)
    side.execute(f"CREATE TABLE IF NOT EXISTS operations ({cols})")
    return side


def _row(side: Optional[sqlite3.Connection], task: str) -> Optional[dict]:
    if side is None:
        return None
    found = side.execute("SELECT * FROM operations WHERE task_id = ?", (task,)).fetchone()
    return dict(found) if found is not None else None


def _update(side: sqlite3.Connection, row: dict, **fields) -> dict:
    fields["updated_at_ms"] = _now_ms()
    side.execute("BEGIN IMMEDIATE")
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        side.execute(f"UPDATE operations SET {sets} WHERE task_id = ?", (*fields.values(), row["task_id"]))
        side.execute("COMMIT")
    except BaseException:
        side.execute("ROLLBACK")
        raise
    row.update(fields)
    return row


def _bump(side: sqlite3.Connection, row: dict, counter: str, maximum: int, state: str) -> bool:
    """Check-and-increment a send budget in one transaction, before the send."""
    side.execute("BEGIN IMMEDIATE")
    try:
        current = side.execute(f"SELECT {counter} FROM operations WHERE task_id = ?",
                               (row["task_id"],)).fetchone()[0]
        if current >= maximum:
            side.execute("COMMIT")
            row[counter] = current
            return False
        now = _now_ms()
        side.execute(f"UPDATE operations SET {counter} = ?, local_state = ?, updated_at_ms = ? WHERE task_id = ?",
                     (current + 1, state, now, row["task_id"]))
        side.execute("COMMIT")
    except BaseException:
        side.execute("ROLLBACK")
        raise
    row.update({counter: current + 1, "local_state": state, "updated_at_ms": now})
    return True


def _identity(row: dict) -> tuple:
    return (row["task_id"], row["run_uid"], row["admission_uid"], row["operation_uid"], row["envelope_digest"])


def _retire(side, row) -> str:
    _update(side, row, local_state="retired_unknown")
    return "retired_unknown"


def _done(side, row, cls: str, body: Any) -> str:
    _update(side, row, local_state="done", last_class=cls, last_body=json.dumps(body, sort_keys=True))
    return cls


def _gate(side, row, hub, board) -> tuple[Optional[str], str]:
    """Preflight receipt rule. Returns (result to return, class); result None means continue."""
    cls, body = _read(hub, board, _identity(row))
    if cls in TERMINAL:
        return _done(side, row, cls, body), cls
    if cls in ("unavailable", "refused"):
        return "unavailable", cls
    if cls == "absent" and row["local_state"] in ("admitted", "executing", "result_held", "settle_sent"):
        return "unavailable", cls
    return None, cls


# --------------------------------------------------------------------- prepare

def _require_platform() -> None:
    if fcntl is None or not hasattr(signal, "SIGALRM"):
        raise RuntimeError("kanban_business_review requires flock and SIGALRM")


def _probe(hub, board: str) -> bool:
    try:
        result = hub._call("POST", f"/v2/boards/{_quoted(board)}/ops", {"ops": [], "probe": True})
    except Exception:  # health: allow BLE001 -- any probe failure means the board is unavailable; nothing is sent
        return False
    return isinstance(result, dict) and result.get("results") == []


def prepare(conn: sqlite3.Connection, hub, board: str, review_task: str) -> str:
    """Drive the one review operation for ``review_task`` as far as it can go."""
    _require_platform()
    base = _base(board)
    lock_dir = base / board
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(lock_dir / f"{review_task}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "unavailable"
        side = _open_sidecar(board, create=False)
        try:
            row = _row(side, review_task)
            if row is None:
                return _start(conn, hub, board, review_task)
            return _drive(conn, hub, board, side, row)
        finally:
            if side is not None:
                side.close()
    finally:
        os.close(lock_fd)


def _start(conn, hub, board: str, review_task: str) -> str:
    reviewer_session = _new_id("rev-")
    bound = _handoff(conn, review_task, reviewer_session)
    if bound is None:
        return "unqualified"
    if not _probe(hub, board):
        return "unavailable"
    run_uid, admission_uid, operation_uid = _new_id("run-"), _new_id("adm-"), _new_id("op-")
    grant_op_id, settle_op_id = _new_id("cop-"), _new_id("cop-")
    envelope = bound["envelope"]
    digest = envelope_digest(envelope, admission_uid, operation_uid)
    work_dir = _base(board) / board / operation_uid
    try:
        os.mkdir(work_dir, 0o700)
        os.chmod(work_dir, 0o700)
        prompt = INSTRUCTION + b"\n" + bound["diff"]
        fd = os.open(str(work_dir / "prompt.txt"), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, prompt)
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        return "unavailable"
    grant = {
        "action": "grant", "task": review_task, "run_uid": run_uid, "lane": "review", "ttl_s": LEASE_TTL_S,
        "client_op_id": grant_op_id,
        "review_admit": {"protocol": 1, "admission_uid": admission_uid, "operation_uid": operation_uid, **envelope},
    }
    row = {c: None for c in COLUMNS}
    row.update(envelope)
    row.update(
        task_id=review_task, source_run_id=bound["source_run_id"], run_uid=run_uid, admission_uid=admission_uid,
        operation_uid=operation_uid, envelope_digest=digest, grant_op_id=grant_op_id, settle_op_id=settle_op_id,
        grant_json=canonical(grant).decode("utf-8"), prompt_sha256=sha256_hex(prompt),
        implementer=bound["implementer"], local_state="grant_sent", work_dir=str(work_dir), grant_attempts=0,
        settle_attempts=0, updated_at_ms=_now_ms(),
    )
    side = _open_sidecar(board, create=True)
    try:
        side.execute("BEGIN IMMEDIATE")
        try:
            side.execute(f"INSERT INTO operations ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                         tuple(row[c] for c in COLUMNS))
            side.execute("COMMIT")
        except BaseException:
            side.execute("ROLLBACK")
            raise
        return _send_grant(conn, hub, board, side, row)
    finally:
        side.close()


def _drive(conn, hub, board, side, row) -> str:
    state = row["local_state"]
    if state == "done":
        return row["last_class"]
    result, cls = _gate(side, row, hub, board)
    if result is not None:
        return result
    if state == "retired_unknown":
        return "retired_unknown"
    if state == "grant_sent":
        return _send_grant(conn, hub, board, side, row)
    if state == "grant_refused":
        if cls == "live":
            _update(side, row, local_state="grant_sent")
            return _send_grant(conn, hub, board, side, row)
        return _resend_refused(conn, hub, board, side, row)
    if state == "admitted":
        return _execute(hub, board, side, row)
    if state == "executing":
        return _recover(hub, board, side, row, have_live=True)
    if state in ("result_held", "settle_sent"):
        return _settle(hub, board, side, row)
    return "unavailable"


def _resend_refused(conn, hub, board, side, row) -> str:
    bound = _handoff(conn, row["task_id"], row["reviewer_session"])
    if bound is None:
        return "refused"
    if envelope_digest(bound["envelope"], row["admission_uid"], row["operation_uid"]) != row["envelope_digest"]:
        return "refused"
    if not _probe(hub, board):
        return "refused"
    grant = json.loads(row["grant_json"])
    grant["client_op_id"] = _new_id("cop-")
    _update(side, row, grant_json=canonical(grant).decode("utf-8"), grant_op_id=grant["client_op_id"],
            local_state="grant_sent")
    return _send_grant(conn, hub, board, side, row)


def _send_grant(conn, hub, board, side, row) -> str:
    if not _bump(side, row, "grant_attempts", GRANT_MAX, "grant_sent"):
        return _retire(side, row)
    try:
        body = hub.leases(board, json.loads(row["grant_json"]))
    except Exception:  # health: allow BLE001 -- a failed grant send is unknown; the receipt read decides, never a resend-as-new
        return "unavailable"
    if not isinstance(body, dict):
        return "unavailable"
    if "review_admission" in body:
        adm = body["review_admission"]
        deadline = adm.get("hard_deadline_ms") if isinstance(adm, dict) else None
        if (isinstance(adm, dict) and adm.get("task") == row["task_id"] and adm.get("run_uid") == row["run_uid"]
                and adm.get("admission_uid") == row["admission_uid"]
                and adm.get("operation_uid") == row["operation_uid"]
                and adm.get("envelope_digest") == row["envelope_digest"]
                and type(deadline) is int and deadline > 0):
            _update(side, row, hard_deadline_ms=deadline, local_state="admitted")
            result, _cls = _gate(side, row, hub, board)
            if result is not None:
                return result
            return _execute(hub, board, side, row)
        return _retire(side, row)
    if body.get("status") == "rejected":
        cls, receipt = _read(hub, board, _identity(row))
        if cls == "absent":
            _update(side, row, local_state="grant_refused")
            return "refused"
        if cls == "live":
            return _retire(side, row)
        if cls in TERMINAL:
            return _done(side, row, cls, receipt)
        return "unavailable"
    return "unavailable"


# ------------------------------------------------------------------- execution

def _child_argv(work_dir: str, wall_seconds: int, prompt_sha256: str) -> list[str]:
    return [sys.executable, "-m", "hermes_cli.kanban_business_review", "--child", str(work_dir),
            str(wall_seconds), prompt_sha256]


def _child_env() -> dict:
    env = dict(os.environ)
    for key in CHILD_ENV_DROP:
        env.pop(key, None)
    return env


def _parent_wait_seconds(wall_seconds: int) -> float:
    return wall_seconds + 10


def _execute(hub, board, side, row) -> str:
    now = _now_ms()
    deadline = row["hard_deadline_ms"]
    if not isinstance(deadline, int) or deadline - now < SPAWN_MARGIN_MS:
        return _retire(side, row)
    work_dir = Path(row["work_dir"])
    try:
        prompt = (work_dir / "prompt.txt").read_bytes()
    except OSError:
        return _retire(side, row)
    if sha256_hex(prompt) != row["prompt_sha256"]:
        return _retire(side, row)
    wall_seconds = min(deadline - now - 60000, 840000) // 1000
    lock_fd = os.open(str(work_dir / "child.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return "unavailable"
    try:
        _update(side, row, local_state="executing")
        try:
            proc = subprocess.Popen(_child_argv(str(work_dir), wall_seconds, row["prompt_sha256"]),
                                    env=_child_env(), pass_fds=(lock_fd,), start_new_session=True,  # detached-spawn: ok — awaited with a group-killing timeout; the child must hold the inherited lock if this parent dies
                                    stdin=subprocess.DEVNULL)
        except Exception:  # health: allow BLE001 -- any spawn failure retires the operation; nothing ran
            return _retire(side, row)
    finally:
        os.close(lock_fd)
    try:
        proc.wait(timeout=_parent_wait_seconds(wall_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX only, _require_platform refuses elsewhere
        except ProcessLookupError:
            pass
        proc.wait()  # health: allow HX006 -- reaping after SIGKILL of the whole group returns promptly
    return _recover(hub, board, side, row, have_live=False)


def _recover(hub, board, side, row, *, have_live: bool) -> str:
    work_dir = Path(row["work_dir"])
    lock_fd = os.open(str(work_dir / "child.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "unavailable"
        result_path = work_dir / "result.json"
        if not result_path.exists():
            return _retire(side, row)
        try:
            raw = result_path.read_bytes()
        except OSError:
            return _retire(side, row)
        verdict = parse_verdict(raw)
        if verdict is None:
            return _retire(side, row)
        provider = model = None
        try:
            route = json.loads((work_dir / "route.json").read_text(encoding="utf-8-sig"))
            if isinstance(route, dict):
                provider = route.get("provider") if isinstance(route.get("provider"), str) else None
                model = route.get("model") if isinstance(route.get("model"), str) else None
        except (OSError, ValueError):
            pass
        op = settlement_op(row, verdict)
        _update(side, row, settle_json=canonical(op).decode("utf-8"), local_state="result_held",
                route_provider=provider, route_model=model)
    finally:
        os.close(lock_fd)
    return _settle(hub, board, side, row, have_live=have_live)


def settlement_op(row: dict, verdict: dict) -> dict:
    summary = verdict["summary"]
    op = {"op": "complete" if verdict["verdict"] == "approve" else "request_changes",
          "task": row["task_id"], "run_uid": row["run_uid"], "operation_uid": row["operation_uid"],
          "capability_digest": row["capability_digest"], "summary": summary, "result": summary}
    if verdict["verdict"] == "request_changes":
        op["reason"] = summary
    op["payload_hash"] = sha256_hex(canonical(op))
    op["client_op_id"] = row["settle_op_id"]
    return op


# ------------------------------------------------------------------ settlement

def _settle(hub, board, side, row, *, have_live: bool = True) -> str:
    if not have_live:
        result, _cls = _gate(side, row, hub, board)
        if result is not None:
            return result
    while True:
        if _now_ms() >= row["hard_deadline_ms"]:
            return _retire(side, row)
        if not _bump(side, row, "settle_attempts", SETTLE_MAX, "settle_sent"):
            return _retire(side, row)
        try:
            hub.ops(board, [json.loads(row["settle_json"])])
        except Exception:  # health: allow BLE001 S110 -- a send error is never an answer; the receipt read below decides
            pass
        cls, body = _read(hub, board, _identity(row))
        if cls in TERMINAL:
            return _done(side, row, cls, body)
        if cls != "live":
            return "unavailable"
        if row["settle_attempts"] >= SETTLE_MAX:
            return _retire(side, row)


# ---------------------------------------------------------------------- output

class _Reject(ValueError):
    pass


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _Reject(f"repeated key {key!r}")
        out[key] = value
    return out


def _no_constant(name):
    raise _Reject(f"non-finite constant {name}")


def _text_ok(value: Any, limit: int) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def parse_verdict(raw: bytes) -> Optional[dict]:
    """A strictly valid verdict object, or ``None``. Never maps bad output to a verdict."""
    if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or set(obj) != {"verdict", "summary", "findings"}:
        return None
    if obj["verdict"] not in ("approve", "request_changes") or not _text_ok(obj["summary"], 4000):
        return None
    findings = obj["findings"]
    if not isinstance(findings, list) or len(findings) > 40:
        return None
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != {"title", "body"}
                or not _text_ok(finding["title"], 2000) or not _text_ok(finding["body"], 2000)):
            return None
    if (obj["verdict"] == "approve") != (findings == []):
        return None
    return obj


# ----------------------------------------------------------------------- child

_CHILD: dict = {"work_dir": None, "wall_seconds": 840}


def _allowed_client_types() -> tuple:
    allowed = []
    try:
        import openai
        allowed.append(openai.OpenAI)
    except Exception:  # health: allow BLE001 S110 -- a client type that cannot load is simply not allowed (fail closed)
        pass
    try:
        from agent import auxiliary_client as aux
        allowed += [aux.CodexAuxiliaryClient, aux.AnthropicAuxiliaryClient, aux.BedrockAuxiliaryClient]
    except Exception:  # health: allow BLE001 S110 -- a client type that cannot load is simply not allowed (fail closed)
        pass
    try:
        from agent.gemini_native_adapter import GeminiNativeClient
        allowed.append(GeminiNativeClient)
    except Exception:  # health: allow BLE001 S110 -- a client type that cannot load is simply not allowed (fail closed)
        pass
    return tuple(allowed)


def _check_request(client: Any, kwargs: Any, allowed: tuple) -> None:
    if not any(type(client) is kind for kind in allowed):
        raise ToolInventoryRefused(f"client type {type(client).__name__} is not a plain request client")
    if not isinstance(kwargs, dict):
        raise ToolInventoryRefused("request kwargs are not a dict")
    if any(key in kwargs for key in FORBIDDEN_KWARGS):
        raise ToolInventoryRefused("request carries a tool key")
    extra = kwargs.get("extra_body")
    if isinstance(extra, dict) and any(key in extra for key in FORBIDDEN_KWARGS):
        raise ToolInventoryRefused("request extra_body carries a tool key")
    if kwargs.get("stream"):
        raise ToolInventoryRefused("streamed request")


def _response_text(resp: Any) -> str:
    try:
        message = resp.choices[0].message
        content = message.content
        tool_calls = getattr(message, "tool_calls", None)
    except Exception as exc:
        raise ToolInventoryRefused("response has no message") from exc
    if tool_calls:
        raise ToolInventoryRefused("response carries tool calls")
    if not isinstance(content, str):
        raise ToolInventoryRefused("response content is not text")
    return content


def complete_fn(messages: list) -> str:
    """Production reviewer call: class selector, no tools, every request guarded."""
    from agent import auxiliary_client as aux
    allowed = _allowed_client_types()
    guarded_texts: list[str] = []
    original = aux._create_with_progress

    def guarded(client, kwargs, *args, **kw):
        _check_request(client, kwargs, allowed)
        resp = original(client, kwargs, *args, **kw)
        guarded_texts.append(_response_text(resp))
        return resp

    route: dict = {}
    aux._create_with_progress = guarded
    try:
        resp = aux.call_llm("kanban_business_review", model="class:deep", messages=messages, tools=None,
                            timeout=_CHILD["wall_seconds"], route_info=route)
    finally:
        aux._create_with_progress = original
    if _CHILD["work_dir"] is not None:
        Path(_CHILD["work_dir"], "route.json").write_text(json.dumps(route, sort_keys=True), encoding="utf-8")
    text = _response_text(resp)
    if not guarded_texts or text != guarded_texts[-1]:
        raise ToolInventoryRefused("returned text did not pass through a guarded request")
    return text


def _tool_inventory() -> list:
    from model_tools import get_tool_definitions
    return get_tool_definitions(enabled_toolsets=[], quiet_mode=True)


def _arm_alarm(wall_seconds: int) -> None:
    signal.signal(signal.SIGALRM, signal.SIG_DFL)  # windows-footgun: ok — POSIX only, _require_platform refuses elsewhere
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})  # windows-footgun: ok — POSIX only
    signal.alarm(wall_seconds)


def _child_body(work_dir: str, wall_seconds: int, prompt_sha256: str) -> int:
    for key in CHILD_ENV_DROP:
        os.environ.pop(key, None)
    _CHILD.update(work_dir=work_dir, wall_seconds=wall_seconds)
    try:
        prompt = Path(work_dir, "prompt.txt").read_bytes()
    except OSError:
        return 5
    if sha256_hex(prompt) != prompt_sha256:
        return 5
    try:
        if _tool_inventory():
            return 3
    except Exception:  # health: allow BLE001 -- any inventory error refuses the review (exit 3)
        return 3
    try:
        text = complete_fn([{"role": "user", "content": prompt.decode("utf-8")}])
        if not isinstance(text, str):
            return 4
    except Exception:  # health: allow BLE001 -- any model-call error is a failed review (exit 4); no verdict is written
        return 4
    tmp = Path(work_dir, "result.json.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, Path(work_dir, "result.json"))
    return 0


def _child_entry(work_dir: str, wall_seconds: int, prompt_sha256: str) -> int:
    _arm_alarm(wall_seconds)
    return _child_body(work_dir, wall_seconds, prompt_sha256)


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "--child":
        return _child_entry(argv[1], int(argv[2]), argv[3])
    print("usage: python -m hermes_cli.kanban_business_review --child WORK_DIR WALL_SECONDS PROMPT_SHA256",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
