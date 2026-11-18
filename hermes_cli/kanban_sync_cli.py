"""``hermes kanban sync``: attach a board to the central Board service, import it, and inspect it. [daveey]

- ``status``  mode, service board, gateway, cursor, outbox depth and the last error.
- ``attach``  bind the board to the service under this gateway's hub name and set its mode (``shadow``/``primary``/``off``).
- ``import``  queue every existing row for upload (idempotent; run once after the first attach).
- ``diff``    this gateway's live cards against the shadow copy of the service (exit 1 when not clean).
- ``run``     one sync round now (the gateway runs it continuously while ``kanban.sync.enabled`` is on).
- ``reconcile`` after the board file lost rows: upload what the service lacks, restore this gateway's rows the
  file lost, then diff (``hermes kanban recover`` runs it after a rebuild).
- ``reserved-key`` one-card correction of a never-created local card whose ``pickup-stall:``/``worker-resources-unknown:``
  idempotency key the Board refused: preview by default, ``--apply --expect TOKEN`` to apply (contract and limits in
  ``kanban_sync_reserved_key``).
"""

from __future__ import annotations

import argparse
import json

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_sync_guard import SyncRoundBusy


def _board(args: argparse.Namespace) -> str:
    return getattr(args, "board", None) or "default"


def _path(slug: str):
    from hermes_cli import kanban_db
    return kanban_db.kanban_db_path(board=slug)


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
        return
    for k, v in obj.items():
        print(f"{k:>18}: {v}")


def _status(args: argparse.Namespace) -> int:
    slug = _board(args)
    conn = kbc.connect(_path(slug))
    try:
        out = {"board": slug, **ksc.status(conn)}
    finally:
        conn.close()
    from hermes_cli.kanban_sync_claims import describe
    out["claims"] = describe(_path(slug), out.get("mode") or "off")  # the dispatcher's first-round gate
    shadow = ksc.shadow_path(slug)
    if shadow.exists():
        sconn = kbc.connect(shadow)
        try:
            out["shadow_cursor"] = (sync.sync_state(sconn) or {}).get("cursor")
        finally:
            sconn.close()
    _print(out, args.json)
    return 0


def _attach(args: argparse.Namespace) -> int:
    slug = _board(args)
    if not sync.sync_enabled() or not sync.board_selected(str(_path(slug))):
        print(f"kanban sync: enable it first (kanban.sync.enabled: true; kanban.sync.boards includes {slug})")
        return 2
    ident = ksc.load_identity()
    conn = kbc.connect(_path(slug))
    try:
        ksc.ensure_identity(conn, slug, ident.gateway)
        sync.set_mode(conn, args.mode)
        print(f"kanban sync: {slug} attached to {ident.url} as {ident.gateway}, mode {args.mode}")
    finally:
        conn.close()
    return 0


def _import(args: argparse.Namespace) -> int:
    slug = _board(args)
    conn = kbc.connect(_path(slug))
    try:
        counts = ksc.import_board(conn)
    finally:
        conn.close()
    cleared = counts.pop("cleared_rejected", 0)
    print(f"kanban sync: queued {sum(counts.values())} rows from {slug} ({', '.join(f'{k}={v}' for k, v in counts.items())})"
          + (f"; cleared {cleared} refused rows they supersede" if cleared else ""))
    return 0


def _diff(args: argparse.Namespace) -> int:
    slug = _board(args)
    ident = ksc.load_identity()
    # Refresh first, before opening the replica. Only when no sync round holds the board (one replica puller per
    # board per host); a busy round's replica is compared as it stands. A diagnostic: it never rebuilds the replica
    # (a Resync is reported as refresh_error) and never transfers attachments (t_01a1d41c2612ebc0).
    refresh_error = None
    pulled = ksc.Pulled()
    try:
        pulled = ksc._pull_replica(slug, ksc.Hub(ident), ident.gateway, _path(slug), diagnostic=True)
    except (ksc.HubError, ksc.Resync, OSError) as exc:
        refresh_error = str(exc)
    else:
        if pulled.more:
            refresh_error = (f"the replica is still behind the feed head after {ksc.PULL_PAGE_BUDGET} pages; the "
                             "sync loop is catching up, rerun later")
    live = kbc.connect(_path(slug))
    shadow = ksc.open_shadow(slug, ident.gateway)
    try:
        report = ksc.diff(live, shadow, ident.gateway)
        if refresh_error is not None:
            report.update(clean=False, unavailable=True, refresh_error=refresh_error)
        elif pulled.busy:
            report["refresh_skipped"] = "a sync round holds the board; compared against its replica as it stands"
    finally:
        live.close()
        shadow.close()
    report = {"board": slug, **report}
    if not args.json:
        for key in ("missing_in_shadow", "extra_in_shadow"):
            report[key] = f"{len(report[key])} {report[key][:10]}" if report[key] else 0
        mismatch = report["field_mismatch"]
        report["field_mismatch"] = f"{len(mismatch)} {dict(list(mismatch.items())[:10])}" if mismatch else 0
    _print(report, args.json)
    return 0 if report["clean"] else 1


def _run(args: argparse.Namespace) -> int:
    results = ksc.sync_all_once()
    rows = [{"board": r.board, "mode": r.mode, "pulled": r.pulled, "pushed": r.push.sent if r.push else 0,
             "rejected": r.push.rejected if r.push else 0, "error": r.error} for r in results]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(" ".join(f"{k}={v}" for k, v in r.items()))
    return 1 if any(r["error"] for r in rows) else 0


def _links(args: argparse.Namespace) -> int:
    """Reconcile this gateway's chat links in every synced board now (the sync loop does this every minute)."""
    ident = ksc.load_identity()
    ksc.reconcile_links(ksc.board_paths(), ident.gateway)
    print("kanban sync: chat links reconciled")
    return 0


def _links_diff(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_chat_links
    from hermes_cli.kanban_chat_links_settle import DEFAULT_SETTLE_S
    settle_s = getattr(args, "settle", None)
    report = kanban_chat_links.links_diff(ksc.load_identity().gateway,
                                          settle_s=DEFAULT_SETTLE_S if settle_s is None else settle_s)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for k in ("clean", "chats", "ledger_links", "replica_links", "machine_links", "profile_board_links"):
            print(f"{k:>16}: {report[k]}")
        for k in ("profile_board", "only_in_ledger", "only_in_replica"):
            items = [(s, c) for s, cards in report[k].items() for c in cards]
            print(f"{k:>16}: {len(items)} {items[:8]}")
        for k, diff in (report.get("in_flight") or {}).items():
            items = [(s, c) for s, cards in diff.items() for c in cards]
            print(f"{'in_flight ' + k.removeprefix('only_in_'):>16}: {len(items)} {items[:8]}")
    return 0 if report["clean"] else 1


def _reconcile(args: argparse.Namespace) -> int:
    slug = _board(args)
    report = ksc.reconcile(slug, _path(slug))
    _print(report, args.json)
    return 0 if report.get("clean") or report.get("mode") == "off" else 1


def _machine_links(args: argparse.Namespace) -> int:
    """Links of this gateway's machine sessions (worker/cron/one-shot/subagent transcripts) still unreleased in each
    synced board (dry run), or, with ``--release``, release them now through the reconcile (receipt under
    ``~/.hermes/kanban/repairs/``). [daveey]"""
    from hermes_cli import kanban_chat_links
    gateway = ksc.load_identity().gateway
    boards = [(s, p) for s, p in ksc.board_paths() if not getattr(args, "board", None) or s == args.board]
    out = {}
    for slug, path in boards:
        conn = kbc.connect(path)
        try:
            if sync.board_mode(conn) == "off":
                continue
            pending = kanban_chat_links.machine_links(conn, slug, gateway)
            if args.release and pending:
                kanban_chat_links.reconcile(conn, slug, gateway)
                left = kanban_chat_links.machine_links(conn, slug, gateway)
                out[slug] = {"released": len(pending) - len(left), "left": len(left), "uids": [p["uid"] for p in pending]}
            else:
                out[slug] = {"pending": len(pending), "uids": [p["uid"] for p in pending]}
        finally:
            conn.close()
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for slug, r in out.items():
            print(f"{slug}: " + " ".join(f"{k}={v}" for k, v in r.items() if k != "uids"))
    return 0


def _refill_history(args: argparse.Namespace) -> int:
    """Refill the event prefix a pre-full-history pickup cut off local copies of other gateways' cards, from this
    board's replica (``kanban_pickup_history``). Exit 1 when a named card could not be refilled completely."""
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_pickup_history import refill
    slug = _board(args)
    conn = kbc.connect(_path(slug))
    try:
        if sync.board_mode(conn) == "off":
            print(f"kanban sync: {slug} does not sync to the Board service; nothing to refill")
            return 2
        replica = wn.open_replica(conn)
        if replica is None:
            print(f"kanban sync: {slug} has no replica yet (run `hermes kanban sync run` first)")
            return 2
        try:
            report = refill(conn, replica, wn._own(conn), args.task_ids or None)
        finally:
            replica.close()
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2))
    elif not report:
        print(f"kanban sync: {slug}: every open copied card already holds its full history")
    else:
        for tid, r in report.items():
            print(f"{tid}: filled={r['filled']} unresolved={r['unresolved']}"
                  + (f" ({r['reason']})" if r.get("reason") else ""))
    return 1 if any(r["unresolved"] or r.get("reason") for r in report.values()) else 0


def _reserved_key(args: argparse.Namespace) -> int:
    """Preview (default) or apply (``--apply --expect TOKEN``) one reserved-key correction; exit 1 when refused."""
    from hermes_cli import kanban_sync_reserved_key as rk
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print("kanban sync: --reason TEXT is required (non-blank)")
        return 2
    if args.apply and not args.expect:
        print("kanban sync: --apply needs --expect TOKEN from the preview")
        return 2
    slug = _board(args)
    hub = ksc.Hub(ksc.load_identity())
    conn = kbc.connect(_path(slug))
    try:
        if args.apply:
            report = rk.apply(conn, hub, slug, args.task_id, args.new_key, args.expect, reason)
        else:
            report = rk.plan(conn, hub, slug, args.task_id, args.new_key, reason)
    finally:
        conn.close()
    _print(report, args.json)
    return 0 if report.get("action") in ("applied", "already_applied") or (
        report.get("action") == "preview" and report.get("eligible")) else 1


_ACTIONS = {"status": _status, "attach": _attach, "import": _import, "diff": _diff, "run": _run,
            "links": _links, "links-diff": _links_diff, "reconcile": _reconcile, "machine-links": _machine_links,
            "refill-history": _refill_history, "reserved-key": _reserved_key}


def cmd_sync(args: argparse.Namespace) -> int:
    action = getattr(args, "sync_action", None)
    if action not in _ACTIONS:
        print("usage: hermes kanban sync {status,attach,import,diff,run,links,links-diff,reconcile,machine-links,"
              "refill-history,reserved-key} [--board B]")
        return 2
    try:
        return _ACTIONS[action](args)
    except (ksc.SyncUnavailable, SyncRoundBusy) as exc:
        print(str(exc))
        return 2
