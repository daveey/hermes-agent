"""``hermes kanban sync``: attach a board to the central Board service, import it, and inspect it. [daveey]

- ``status``  mode, service board, gateway, cursor, outbox depth and the last error.
- ``attach``  bind the board to the service under this gateway's hub name and set its mode (``shadow``/``primary``/``off``).
- ``import``  queue every existing row for upload (idempotent; run once after the first attach).
- ``diff``    this gateway's live cards against the shadow copy of the service (exit 1 when not clean).
- ``run``     one sync round now (the gateway runs it continuously while ``kanban.sync.enabled`` is on).
"""

from __future__ import annotations

import argparse
import json

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc


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
    live = kbc.connect(_path(slug))
    shadow = ksc.open_shadow(slug, ident.gateway)
    try:
        try:  # compare against the service's current state, not the replica's last round
            ksc.pull(shadow, ksc.Hub(ident), slug, observers=False)
        except (ksc.HubError, ksc.Resync, OSError) as exc:
            print(f"kanban sync: could not refresh the replica first ({exc}); comparing with its last round")
        report = ksc.diff(live, shadow, ident.gateway)
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
    report = kanban_chat_links.links_diff(ksc.load_identity().gateway)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for k in ("clean", "chats", "ledger_links", "replica_links"):
            print(f"{k:>16}: {report[k]}")
        for k in ("only_in_ledger", "only_in_replica"):
            items = [(s, c) for s, cards in report[k].items() for c in cards]
            print(f"{k:>16}: {len(items)} {items[:8]}")
    return 0 if report["clean"] else 1


_ACTIONS = {"status": _status, "attach": _attach, "import": _import, "diff": _diff, "run": _run,
            "links": _links, "links-diff": _links_diff}


def cmd_sync(args: argparse.Namespace) -> int:
    action = getattr(args, "sync_action", None)
    if action not in _ACTIONS:
        print("usage: hermes kanban sync {status,attach,import,diff,run,links,links-diff} [--board B]")
        return 2
    try:
        return _ACTIONS[action](args)
    except ksc.SyncUnavailable as exc:
        print(str(exc))
        return 2
