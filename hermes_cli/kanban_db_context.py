"""Worker-context rendering for ``build_worker_context``: the capped markdown sections a spawned
worker reads about its task (header, attachments, prior attempts, parent handoffs, role
history, comments).

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached late-bound via
``_kb`` so monkeypatching ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Optional

import hermes_cli.kanban_db as _kb

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Attachment, Task

# build_worker_context() caps, sized for a ~100k-char prompt with headroom.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_PARENT_RESULTS = 8       # most recently completed N parent handoffs shown in full [daveey]
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # per comment


def _ctx_standing(lines: list[str], task: Task, comments: list) -> None:
    """Preserve explicit standing instructions even when comment history is capped.

    Direct promotion path: every candidate representation and the final formatted line must pass the
    unchanged model-neutral recognizer. The raw body/comments still render below."""
    from hermes_cli.kanban_model_neutral import standing_allowed, standing_candidates
    found = standing_candidates(task.body or "", *(c.body or "" for c in comments))
    rendered = [f"standing: {words}" for raw, words in found if standing_allowed(raw, f"standing: {words}")]
    if not rendered:
        return
    lines.append("## Standing instructions (from the user; in force for this card)")
    lines += rendered
    lines.append("(These still govern this run: e.g. a standing \"don't wait\" means decide it yourself instead "
                 "of blocking on the user, unless only a human can act — a credential, a physical step.)")
    lines.append("")


_RELEASED_BY = {"human": "a human operator", "chat": "the card's owning chat", "delegate": "a delegate chat"}


def _ctx_release(lines: list[str], conn: sqlite3.Connection, task_id: str, now: Optional[int] = None) -> None:
    """The release that governs this run, ahead of the prior attempts and the comment thread. [daveey]

    On 2026-11-24/25, 70 of 137 releases were followed by another stop (median 158 min). The mandate sat under
    ~30k characters of earlier runs' stop summaries, framed "stop again if the work needs more"; the next runs
    re-read those stops as instructions. A newer stop or any execution restriction hides this section
    (``current_mandate``)."""
    from hermes_cli.kanban_release import current_mandate
    from hermes_cli.kanban_scope_renewal import MAX_MANDATE
    released = current_mandate(conn, task_id)
    if not released:
        _ctx_human(lines, conn, task_id, now, released=False)
        return
    who = _RELEASED_BY.get(str(released.get("authority_kind")), "an authorized releaser")
    lines += ["## Release mandate (governs this run)",
              f"This card was released after its last stop by {who} (comment {released['comment_id']}). The "
              "mandate below is this run's scope. It supersedes stop rules agents wrote in older comments or "
              "mandates; the stop reasons of earlier runs further down are history, not instructions. Safety "
              "exclusions in the card body and a human's stop instructions still hold. Findings, "
              "design changes, review CHANGES, red or pending CI and sandbox refusals are work inside it. When "
              "the acceptance is met, finish with `kanban_complete` (or `kanban_request_review`), not "
              "`kanban_retain`.",
              _ctx_cap(released["body"], MAX_MANDATE), ""]
    _ctx_human(lines, conn, task_id, now, released=True)


_HUMAN_MAX, _HUMAN_BYTES = 10, 1024


def _human_comments(conn: sqlite3.Connection, task_id: str, comments: list) -> list:
    """Those of ``comments`` whose ``commented`` event proves David's words (``kanban_release.by_person``).
    A comment with no such event, or a kind-less one, stays in the ordinary thread only."""
    from hermes_cli.kanban_release import by_person
    by_id, by_uid = {}, {}
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented'", (task_id,)):
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict):
            by_id.setdefault(p.get("comment_id"), p)
            if p.get("comment_uid"):
                by_uid.setdefault(p["comment_uid"], p)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_comments)")}
    uids = dict(conn.execute("SELECT id, uid FROM task_comments WHERE task_id=?", (task_id,))) if "uid" in cols else {}
    # A synced comment binds by uid (its comment_id is the writer board's local id).
    return [c for c in comments if by_person(by_uid.get(uids[c.id], {}) if uids.get(c.id) else by_id.get(c.id, {}))]


def _scope_history(conn: sqlite3.Connection, task_id: str) -> bool:
    """The card was ever scope-stopped, renewed or released."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND (kind IN ('scope_renewed','stop_released') OR "
        "(kind='spend_budget_triaged' AND json_valid(payload) AND json_extract(payload,'$.cause')='scope_stopped')) "
        "LIMIT 1", (task_id,)).fetchone() is not None


def _ctx_human(lines: list[str], conn: sqlite3.Connection, task_id: str, now: Optional[int], *,
               released: bool) -> None:
    """David's comments older than the comment thread's tail (``_CTX_MAX_COMMENTS``), newest ``_HUMAN_MAX`` at
    ``_HUMAN_BYTES`` each, so an early "do not deploy" stays in view under the release mandate that says a
    human's stop instructions still hold. Only on a released or scope-stopped card: ordinary prompts are
    unchanged. [daveey]"""
    if not released and not _scope_history(conn, task_id):
        return
    older = _kb.list_comments(conn, task_id)[:-_CTX_MAX_COMMENTS]
    human = _human_comments(conn, task_id, older) if older else []
    if not human:
        return
    shown = human[-_HUMAN_MAX:]
    lines.append("## Instructions David wrote on this card (older than the thread below)")
    if len(human) > len(shown):
        lines.append(f"_({len(human) - len(shown)} more not shown)_")
    now = int(time.time()) if now is None else now
    for c in shown:
        lines.append(f"comment from `{(c.author or '').replace('`', '')}` at {_ctx_stamp(c.created_at, now)}:")
        lines.append(_ctx_cap(c.body, _HUMAN_BYTES))
    lines.append("")


def _relative_age(ts: Optional[int], now: Optional[int] = None) -> str:
    """``just now`` / ``18h ago`` / ``3d ago``; "" for a missing/invalid ts. An LLM
    reads a bare absolute timestamp as current fact — the relative age is what
    prompts a worker to re-verify stale sibling work."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if now is None:
        now = int(time.time())
    delta = now - ts
    if delta < 60:  # includes negative = clock skew across machines; never claim "in the future"
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _ctx_cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
    """Truncate to ``limit`` chars with a visible ellipsis."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def _ctx_stamp(ts: int, now: int) -> str:
    """``YYYY-MM-DD HH:MM`` plus a relative age when one is available."""
    disp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    age = _relative_age(ts, now)
    return f"{disp}, {age}" if age else disp


def _ctx_metadata_line(metadata: Any) -> Optional[str]:
    if not metadata:
        return None
    from hermes_cli.kanban_artifact_receipt import display_metadata

    try:
        shown = display_metadata(metadata)
        return f"_metadata_: `{_ctx_cap(json.dumps(shown, ensure_ascii=False, sort_keys=True))}`"
    except Exception:
        return None


def _ctx_tail(items: list, cap: int, noun: str) -> tuple[list, Optional[str]]:
    """Keep the newest ``cap`` items; describe the omitted head, if any."""
    omitted = max(0, len(items) - cap)
    if not omitted:
        return items, None
    return items[-cap:], (
        f"_({omitted} earlier {noun}{'s' if omitted != 1 else ''} "
        f"omitted; showing most recent {cap})_"
    )


def ctx_workspace_span(task: Task) -> list:
    """``[offset, line]`` of the header's ``Workspace:`` line in ``build_worker_context(task)``: located by
    header STRUCTURE (the line list ``_ctx_header`` emits; the title is one element even when it holds
    newlines), never by searching the text, so a title or body that contains a ``## Body`` heading or a
    ``Workspace:`` line can not move or shadow it. [daveey]"""
    lines: list[str] = []
    _ctx_header(lines, task)
    k = next(i for i, line in enumerate(lines) if i > 0 and line.startswith("Workspace: "))
    return [len("\n".join(lines[:k])) + 1, lines[k]]


def _ctx_header(lines: list[str], task: Task, *, conn: Optional[sqlite3.Connection] = None) -> None:
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        terminal_timeout = _kb._worker_terminal_timeout_env(
            task.max_runtime_seconds, os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    if conn is not None:  # after Workspace, so ctx_workspace_span (no conn) keeps the same offset [daveey]
        from hermes_cli.kanban_actionability_evidence import context
        context(lines, conn, task)
        from hermes_cli.kanban_wake_summary import context_line
        if wake := context_line(conn, task):
            lines.append(wake)
    lines.append("")
    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_ctx_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")


def _ctx_attachments(lines: list[str], attachments: list[Attachment]) -> None:
    """Absolute on-disk paths so the worker's file tools read them directly
    (remote terminal backends need the attachments dir mounted)."""
    if not attachments:
        return
    lines.append("## Attachments")
    lines.append(
        "Files attached to this task. Read them with the file/terminal "
        "tools at the absolute paths below:"
    )
    for att in attachments:
        size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
        size_str = f", {size_kb} KB" if size_kb else ""
        ctype = f", {att.content_type}" if att.content_type else ""
        lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
    lines.append("")


def _ctx_prior_attempts(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Closed runs on this task (the active run is this worker), newest
    ``_CTX_MAX_PRIOR_ATTEMPTS`` in full, older ones as a one-line marker."""
    all_prior = [r for r in _kb.list_runs(conn, task_id) if r.ended_at is not None]
    shown, omitted_note = _ctx_tail(all_prior, _CTX_MAX_PRIOR_ATTEMPTS, "attempt")
    if not shown:
        return
    first_shown_idx = len(all_prior) - len(shown) + 1
    lines.append("## Prior attempts on this task")
    if omitted_note:
        lines.append(omitted_note)
    for offset, run in enumerate(shown):
        profile = run.profile or "(unknown)"
        outcome = run.outcome or run.status
        lines.append(
            f"### Attempt {first_shown_idx + offset} — {outcome} ({profile}, {_ctx_stamp(run.started_at, now)})"
        )
        if run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        if run.error and run.error.strip():
            lines.append(f"_error_: {_ctx_cap(run.error)}")
        meta_line = _ctx_metadata_line(run.metadata)
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_parent_results(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Done-parent handoffs: newest ``completed`` run's summary+metadata,
    falling back to ``task.result`` for pre-runs-table data. Stamped with a
    relative age so the worker re-verifies stale upstream results."""
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id", (task_id,),
    ).fetchall()
    done: list[tuple] = []
    for pid in (r["parent_id"] for r in parent_rows):
        pt = _kb.get_task(conn, pid)
        if not pt or pt.status != "done":
            continue
        runs = [r for r in _kb.list_runs(conn, pid) if r.outcome == "completed"]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        run = runs[0] if runs else None
        done_ts = run.ended_at if run is not None and run.ended_at else (pt.completed_at or None)
        done.append((done_ts or 0, pid, pt, run))
    # [daveey] A long-lived owner card collects finished prerequisites as parents (30 on t_01a1a1dea9c64d6a,
    # 42 KB of a 99 KB context). Oldest handoffs are the stalest: keep the newest in full, name the rest.
    older: list[tuple] = []
    if len(done) > _CTX_MAX_PARENT_RESULTS:
        by_age = sorted(done, key=lambda d: (d[0], d[1]))
        older = by_age[:-_CTX_MAX_PARENT_RESULTS]
        keep = {d[1] for d in by_age[-_CTX_MAX_PARENT_RESULTS:]}
        done = [d for d in done if d[1] in keep]
    wrote_header = False
    for _ts, pid, pt, run in done:
        if not wrote_header:
            lines.append("## Parent task results")
            lines.append(
                "_Handoffs from upstream tasks, captured when each parent "
                "completed (see age below). These are point-in-time "
                "snapshots, not live state — if a result drives your "
                "current work and it's not recent, re-verify against the "
                "source before acting on it as current._"
            )
            wrote_header = True
        done_ts = run.ended_at if run is not None and run.ended_at else (pt.completed_at or None)
        age = _relative_age(done_ts, now)
        lines.append(f"### {pid}" + (f" (completed {age})" if age else ""))
        if run is not None and run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        elif pt.result:
            lines.append(_ctx_cap(pt.result))
        else:
            lines.append("(no result recorded)")
        meta_line = _ctx_metadata_line(run.metadata) if run is not None else None
        if meta_line:
            lines.append(meta_line)
        lines.append("")
    if older:
        lines.append(
            f"_({len(older)} earlier parent result{'s' if len(older) != 1 else ''} omitted; showing the "
            f"{_CTX_MAX_PARENT_RESULTS} most recently completed. Read one with `hermes kanban show <id>`: "
            + ", ".join(d[1] for d in older) + ")_"
        )
        lines.append("")


def _ctx_role_history(lines: list[str], conn: sqlite3.Connection, task: Task, now: int) -> None:
    """The assignee's 5 most recent completed runs on OTHER tasks — implicit
    role continuity without wiring anything into SOUL.md / MEMORY.md."""
    if not task.assignee:
        return
    role_rows = conn.execute(
        "SELECT t.id, t.title, r.summary, r.ended_at "
        "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
        "WHERE r.profile = ? AND r.task_id != ? "
        "  AND r.outcome = 'completed' "
        "ORDER BY r.ended_at DESC LIMIT 5", (task.assignee, task.id),
    ).fetchall()
    if not role_rows:
        return
    lines.append(f"## Recent work by @{task.assignee}")
    for row in role_rows:
        first = _kb._first_line(row["summary"], 200) or "(no summary)"
        lines.append(
            f"- {row['id']} — {row['title']} ({_ctx_stamp(_kb._to_epoch(row['ended_at']) or 0, now)}): {first}"
        )
    lines.append("")


def _ctx_comments(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Newest ``_CTX_MAX_COMMENTS`` comments. The explicit "comment from
    worker" framing stops an operator-controlled HERMES_PROFILE like
    "hermes-system" being read as a system directive above an
    attacker-influenceable body (defense-in-depth). A comment whose ``commented`` event
    says an agent wrote it (``writer_kind='agent'``) is tagged ``(agent chat)``: David and
    his agents share one profile, so the author alone can't tell them apart. [daveey]"""
    from hermes_cli.kanban_release import current_mandate
    from hermes_cli.kanban_scope_renewal import mandate as _scope_mandate
    renewed = _scope_mandate(conn, task_id)
    released = current_mandate(conn, task_id)
    if released and renewed and renewed["mandate_comment_id"] == released["comment_id"]:
        renewed = None  # already rendered at the top as the release mandate (_ctx_release)
    if renewed:  # in full: this comment is the scope the run was released for, not thread history
        lines += ["## Scope renewal mandate",
                  f"An earlier run stopped this card's scope. It was renewed ({renewed['authority_kind']}, comment "
                  f"{renewed['mandate_comment_id']}) for exactly the scope below; stop again if the work needs more.",
                  renewed["body"], ""]
    shown, omitted_note = _ctx_tail(_kb.list_comments(conn, task_id), _CTX_MAX_COMMENTS, "comment")
    if not shown:
        return
    agent_ids = _kb.agent_comment_ids(conn, task_id, [c.id for c in shown])
    lines.append("## Comment thread")
    if omitted_note:
        lines.append(omitted_note)
    for c in shown:
        # Render author with explicit "comment from worker" framing so operator-controlled HERMES_PROFILE
        # values like "hermes-system" or "operator" can't be misread by the next worker as a system
        # directive above the (attacker-influenceable) comment body. Defense-in-depth — the LLM-controlled
        # author-forgery surface was already closed in #22435. See #22452.
        safe_author = (c.author or "").replace("`", "")
        tag = " (agent chat)" if c.id in agent_ids else ""
        lines.append(f"comment from worker `{safe_author}`{tag} at {_ctx_stamp(c.created_at, now)}:")
        lines.append(_ctx_cap(c.body, _CTX_MAX_COMMENT_BYTES))
        lines.append("")
