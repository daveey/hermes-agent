"""Canonical pure projections for profile sessions, sidebar and stamps.

Producer contract: supply the exact DB-selected rows (including pinned backfill),
counts and scan diagnostics, with profile/active/outcome/automated stamps already
applied. These functions do NOT emulate SQL source, archive, lineage or policy
filters, read stores/clocks/config, or invoke activity annotators. Inputs are
JSON-like values; returned mutable values are detached copies.

Capture one frozen clock for pre-stamping and annotation. ``is_active`` expires
at last_active + 300s (started_at fallback); producers must invalidate at that
boundary even without a DB write. Automated-source policy/config is also an
explicit dependency: a config change must refresh pre-stamped rows. Preserve
profile discovery order and each DB reader's row order in the captured input;
equal sort keys retain that order, not an invented ID tie-breaker.

List pipeline: select_list_window -> caller annotates -> assemble_sessions.
Sidebar pipeline: sidebar_failed -> select_sidebar_window per slice -> caller
annotates -> assemble_sidebar. Skip selection/annotation on a wholly failed scan.
The annotation callable contract is ``annotate(rows: list[dict]) -> list[dict]``
(mutating rows is allowed); it is executed by the caller, NEVER here. Legacy
list order is kanban(display=True), goal, cron, background, turn, subagent,
review; sidebar/stamps order is subagent, kanban(display=True), goal, cron,
background, turn, review. Producers must supply those annotation outputs rather
than trying to derive activity from session DB rows alone.

Stamps pipeline: capture each row's keys AFTER pre-stamping and BEFORE activity
annotation, then pass the annotated rows and positional before-key sets to
assemble_stamps. Missing requested IDs are already omitted by the reader.
"""
from copy import deepcopy
from typing import Any

from hermes_cli.web_server_gateway import _strip_session_list_rows

SESSION_STAMP_COLUMNS = ("cwd", "git_branch", "git_repo_root", "outcome", "muted")
SIDEBAR_SLICES = ("recents", "cron", "messaging")


def _pinned_window(rows, offset, limit):
    window = rows[offset:offset + limit]
    if len(rows) > offset + limit:
        seen = {id(row) for row in window}
        window.extend(row for row in rows[offset + limit:]
                      if row.get("pinned") and id(row) not in seen)
    return window


def select_list_window(rows: list[dict[str, Any]], limit: int, offset: int,
                       order: str, archived: str, full: bool) -> list[dict[str, Any]]:
    """Sort preselected rows stably, page and retain pins; strip before annotation.

    ``archived='only'`` reserves cursor positions for archived rows, appending
    live pinned backfill afterwards. Other archive filtering belongs to the DB.
    ``limit``/``offset`` and enum values must already be route-validated.
    """
    key = "last_active" if order == "recent" else "started_at"
    merged = sorted(rows, key=lambda row: row.get(key) or row.get("started_at") or 0, reverse=True)
    if archived == "only":
        window = _pinned_window([row for row in merged if row.get("archived")], offset, limit)
        window.extend(row for row in merged if row.get("pinned") and not row.get("archived"))
    else:
        window = _pinned_window(merged, offset, limit)
    window = deepcopy(window)
    if not full:
        _strip_session_list_rows(window)
    return window


def assemble_sessions(window, totals, limit, offset, errors, storage) -> dict[str, Any]:
    """Assemble an already annotated window; totals are authoritative SQL counts."""
    return deepcopy({"sessions": window, "total": sum(totals.values()), "profile_totals": totals,
                     "limit": limit, "offset": offset, "errors": errors, "storage": storage})


def select_sidebar_window(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Select one preselected sidebar slice, before activity annotation."""
    return select_list_window(rows, limit, 0, "recent", "exclude", False)


def sidebar_failed(recents_scope, retryable, storage, scanned, contributed) -> bool:
    """Whether the entire scan failed. ``scanned`` contains profile NAMES only.

    ``retryable`` is supplied by the caller's storage-error classification;
    ``contributed`` names profiles with any successfully read nonempty slice.
    """
    failed_names = {err["profile"] for err in retryable if err.get("profile")}
    live = [name for name in scanned if name not in storage]
    if recents_scope != "all":
        return recents_scope in failed_names and recents_scope not in contributed
    return bool(live) and not contributed and all(name in failed_names for name in live)


def assemble_sidebar(windows, *, recents_scope, errors, retryable, storage,
                     scanned, contributed, truncated, usage, messaging_total) -> dict[str, Any]:
    """Assemble annotated slices and failed-load envelopes without source reads.

    ``windows`` maps recents/cron/messaging to annotated rows (may be empty when
    sidebar_failed is true). ``messaging_total`` is the pre-window row count,
    NOT a SQL total. ``truncated`` and ``usage`` are per-profile scan outputs.
    ``storage`` is the corrupt-store mapping; ``scanned`` is ordered names.
    """
    if sidebar_failed(recents_scope, retryable, storage, scanned, contributed):
        body = {key: {"failed": True, "retry": True, "errors": retryable} for key in SIDEBAR_SLICES}
        body["recents"].update(profiles_truncated={}, profiles_usage={})
        body["messaging"]["total"] = 0
    else:
        body = {key: {"sessions": windows[key]} for key in SIDEBAR_SLICES}
        body["recents"].update(profiles_truncated=truncated, profiles_usage=usage)
        body["messaging"]["total"] = messaging_total
        # Preserve legacy JSON insertion order, including partial failures.
        body.update(errors=errors, storage=storage)
        failed = {err["profile"]: {"failed": True, "retry": True, "error": err.get("error", "")}
                  for err in retryable if err.get("profile")}
        if failed:
            body["profiles_failed"] = failed
            for key in SIDEBAR_SLICES:
                body[key]["profiles_failed"] = failed
                body[key]["errors"] = [dict(err) for err in retryable]
    body.update(errors=errors, storage=storage)
    return deepcopy(body)


def assemble_stamps(rows, before_keys, profile: str) -> dict[str, Any]:
    """Select newly introduced public annotation keys plus fixed stored columns.

    ``before_keys`` is positional, one key set per annotated row. Annotation
    must preserve row order and cardinality; mismatched lengths raise ValueError.
    Overwrites of pre-existing keys are intentionally absent unless the key is
    a fixed stamp column. Private annotation keys never enter the response.
    """
    out = []
    for row, before in zip(rows, before_keys, strict=True):
        stamps = {key: row[key] for key in row if key not in before and not key.startswith("_")}
        stamps.update({key: row.get(key) for key in SESSION_STAMP_COLUMNS})
        out.append({"id": row["id"], "profile": profile, **stamps})
    return deepcopy({"sessions": out})
