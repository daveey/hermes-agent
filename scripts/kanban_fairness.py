#!/usr/bin/env python3
"""Operator CLI for the queue-fairness evidence capture (design: docs/kanban-fairness-capture.md). [daveey]

usage: scripts/hermes-python scripts/kanban_fairness.py <command> ...

  capture arm --out DIR --capture-id ID (--db PATH | --board SLUG [--replica]) [--clock-error-s S]
              [--apply-lag-s S] [--unclaimed-exit unknown|elapsed_ready] [--replace]
                    start journaling one Kanban database; writes DIR/plan.json and the opening segment
  capture status --out DIR      the journal's counters and trigger state
  capture drain  --out DIR      copy new journal rows into DIR (run it periodically on long windows)
  capture seal   --out DIR      stop the capture, drain the tail, write DIR/manifest.json
  capture clear  --out DIR      remove the journal tables from the database (the files in DIR stay)
  validate --input DIR          is the capture complete? lists every coverage gap
  report --mode live --input DIR --output FILE [--min-window-s S]
                    cohorts and the four gates (p0 median, max wait while claims, alarms, owners)
  inventory scan --tree NAME=PATH ... --output FILE
  inventory check --scan FILE --classification FILE --output FILE
  inventory roster --manifest FILE --output FILE  validate fleet identity/coverage evidence
  export --input DIR --output FILE.tar.gz

Exit: 0 pass / completed IO, 1 a proven measured failure, 2 indeterminate or unsupported input, 3 tool error.
Nothing here is a model tool, and no scheduler code reads what it writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys

from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_fairness_inventory as inventory
from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_metrics as metrics
from hermes_cli import kanban_fairness_store as store
from hermes_cli.kanban_fairness_schema import EXIT, EXIT_ERROR, INDETERMINATE, PASS

PLAN = "plan.json"


def _connect(db: str):
    from hermes_cli import kanban_db_connect

    return closing(kanban_db_connect.connect(Path(db)))


def _plan(out: Path) -> dict:
    return json.loads((out / PLAN).read_text(encoding="utf-8-sig"))


def _emit(value) -> None:
    print(json.dumps(value, indent=1, sort_keys=True))


def _policy() -> dict:
    """What scored the queue while the capture ran, as far as this checkout can say."""
    from hermes_cli import kanban_admission_score as score

    scorer = Path(score.__file__).read_bytes()
    try:
        rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        rev = None
    return {"core_revision": rev, "scorer_sha256": hashlib.sha256(scorer).hexdigest(),
            "alarm_prefixes": list(score.ALARM_KEY_PREFIXES)}


def _arm(args) -> int:
    out = Path(args.out)
    if args.writer_clocks and not args.db:
        raise ValueError("writer clocks require explicit --db and exclusively controlled upgraded writers")
    clock_error_ns = None
    if args.writer_clocks:
        scaled = args.clock_error_s * 1_000_000_000
        if not 0 <= scaled < 2**63:
            raise ValueError("clock-error-s must yield a nonnegative signed-64-bit nanosecond bound")
        clock_error_ns = int(scaled)
    if args.db:
        db = args.db
    else:
        from hermes_cli import kanban_db
        from hermes_cli.kanban_sync_client import shadow_path

        db = str(shadow_path(args.board) if args.replica else kanban_db.kanban_db_path(args.board))
    if (out / PLAN).exists() and not args.replace:
        print(f"{out / PLAN} exists: this directory already holds a capture", file=sys.stderr)
        return EXIT_ERROR
    from hermes_cli.config import load_config
    from hermes_cli.kanban_fairness_config import DEFAULTS, resolve
    from hermes_cli import kanban_fairness_budget as budget

    config = load_config()
    resolve(config)  # Validate the configured ceiling even when runtime capture is off.
    maximum = config.get("kanban", {}).get("fairness_capture", {}).get("max_bytes", DEFAULTS["max_bytes"])
    budget.initialize(out, maximum)
    plan_path = out / PLAN
    previous = plan_path.read_bytes() if plan_path.exists() else None
    plan = {"capture_id": args.capture_id, "db": db, "board": args.board, "replica": bool(args.replica),
            "assumptions": {"clock_error_s": args.clock_error_s, "apply_lag_s": args.apply_lag_s,
                            "unclaimed_exit": args.unclaimed_exit}}
    budget.write(plan_path, json.dumps(plan, indent=1, sort_keys=True).encode("utf-8"))
    armed = False
    try:
        with _connect(db) as conn:
            schema_before = conn.execute("PRAGMA schema_version").fetchone()[0]
            # arm always creates (or replaces) its tables transactionally. The
            # schema cookie witnesses commit even for same-id, same-ms replacement.
            armed = True  # Keep the plan unless rollback is positively established.
            try:
                state = journal.arm(conn, args.capture_id, replace=args.replace,
                                    clock_error_ns=clock_error_ns)
            except BaseException:
                conn.rollback()  # Also discard a transaction interrupted by Ctrl-C.
                armed = conn.execute("PRAGMA schema_version").fetchone()[0] != schema_before
                raise
            store.drain(conn, out)
    except BaseException:
        # Budget admission comes first; only roll back before the journal is armed.
        if not armed:
            if previous is None:
                plan_path.unlink(missing_ok=True)
            else:
                budget.write(plan_path, previous)
        raise
    _emit({"armed": state, "plan": plan, "acceptance_ready": False})
    return 0


def _capture(args) -> int:
    if args.action == "arm":
        return _arm(args)
    out = Path(args.out)
    plan = _plan(out)
    with _connect(plan["db"]) as conn:
        if args.action == "status":
            _emit({"journal": journal.status(conn), "plan": plan})
        elif args.action == "drain":
            _emit({"receipts": store.drain(conn, out), "journal": journal.status(conn)})
        elif args.action == "seal":
            manifest = store.write_manifest(conn, out, board=plan["board"], assumptions=plan["assumptions"],
                                            policy=_policy(), roster=plan.get("roster"))
            _emit({"manifest": manifest, "acceptance_ready": False})
        else:
            journal.clear(conn)
            _emit({"cleared": plan["db"]})
    return 0


def _validate(args) -> int:
    capture = store.load(Path(args.input))
    _emit({"capture_id": capture["manifest"].get("capture_id"), "records": len(capture["records"]),
           "owner_journal": capture["owners"] is not None, "complete": not capture["gaps"],
           "gaps": capture["gaps"]})
    return EXIT[PASS] if not capture["gaps"] else EXIT[INDETERMINATE]


def _report(args) -> int:
    capture = store.load(Path(args.input))
    prefixes = (capture["manifest"].get("policy") or {}).get("alarm_prefixes")
    if not prefixes:
        from hermes_cli.kanban_admission_score import ALARM_KEY_PREFIXES as prefixes
    verdict = metrics.verdict(capture, alarm_prefixes=tuple(prefixes), min_window_s=args.min_window_s)
    Path(args.output).write_text(json.dumps(verdict, indent=1, sort_keys=True), encoding="utf-8")
    _emit({"overall": verdict["overall"], "coverage_complete": verdict["coverage"]["complete"],
           "gates": {name: gate["status"] for name, gate in verdict["gates"].items()}, "output": args.output})
    return EXIT[verdict["overall"]]


def _inventory(args) -> int:
    if args.action == "roster":
        from hermes_cli.kanban_fairness_roster import validate

        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
        result = validate(manifest.get("roster") if isinstance(manifest, dict) else None)
        Path(args.output).write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
        _emit(result)
        return EXIT[PASS] if result["complete"] else EXIT[INDETERMINATE]
    if args.action == "scan":
        trees = dict(item.split("=", 1) for item in args.tree)
        result = inventory.scan({name: Path(path) for name, path in trees.items()})
        code = 0
    else:
        result = inventory.check(json.loads(Path(args.scan).read_text(encoding="utf-8-sig")),
                                 json.loads(Path(args.classification).read_text(encoding="utf-8-sig")))
        code = EXIT[PASS] if result["complete"] else EXIT[INDETERMINATE]
    Path(args.output).write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    _emit({"output": args.output, "producers": len(result["producers"]), "alarms": len(result["alarms"]),
           "problems": len(result.get("problems", [])), "trees": result["trees"]})
    return code


def _export(args) -> int:
    from hermes_cli.kanban_fairness_root import export

    export(Path(args.input), Path(args.output))
    _emit({"output": args.output, "acceptance_ready": False})
    return 0


def _runtime(args) -> int:
    from hermes_cli import kanban_fairness_activation as activation
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home

    config, home = load_config(), get_hermes_home()
    if args.action == "arm":
        activation.arm(config, home=home, board=args.board, source_gateway=args.source_gateway,
                       board_uid=args.board_uid, clock_error_ns=args.clock_error_ns)
    else:
        activation.disarm(config, home=home, directory=args.directory)
    _emit({"runtime": args.action, "acceptance_ready": False})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    runtime = sub.add_parser("runtime")
    runtime.add_argument("action", choices=("arm", "disarm"))
    runtime.add_argument("--board", default="default")
    runtime.add_argument("--source-gateway")
    runtime.add_argument("--board-uid")
    runtime.add_argument("--directory", help="existing capture directory for disarm after config reset")
    runtime.add_argument("--clock-error-ns", type=int, default=None)
    runtime.set_defaults(run=_runtime)
    capture = sub.add_parser("capture")
    capture.add_argument("action", choices=("arm", "status", "drain", "seal", "clear"))
    capture.add_argument("--out", required=True)
    capture.add_argument("--capture-id")
    capture.add_argument("--db")
    capture.add_argument("--board", default="default")
    capture.add_argument("--replica", action="store_true")
    capture.add_argument("--clock-error-s", type=float, default=2.0)
    capture.add_argument("--writer-clocks", action="store_true",
                         help="sample clocks in writer transactions; ALL writers must use upgraded native connections")
    capture.add_argument("--apply-lag-s", type=float, default=None)
    capture.add_argument("--unclaimed-exit", choices=("unknown", "elapsed_ready"), default="unknown")
    capture.add_argument("--replace", action="store_true")
    capture.set_defaults(run=_capture)
    validate = sub.add_parser("validate")
    validate.add_argument("--input", required=True)
    validate.set_defaults(run=_validate)
    report = sub.add_parser("report")
    report.add_argument("--mode", choices=("live",), required=True)
    report.add_argument("--input", required=True)
    report.add_argument("--output", required=True)
    report.add_argument("--min-window-s", type=float, default=0)
    report.set_defaults(run=_report)
    inv = sub.add_parser("inventory")
    inv.add_argument("action", choices=("scan", "check", "roster"))
    inv.add_argument("--manifest")
    inv.add_argument("--tree", action="append", default=[])
    inv.add_argument("--scan")
    inv.add_argument("--classification")
    inv.add_argument("--output", required=True)
    inv.set_defaults(run=_inventory)
    export = sub.add_parser("export")
    export.add_argument("--input", required=True)
    export.add_argument("--output", required=True)
    export.set_defaults(run=_export)
    return parser


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "capture" and args.action == "arm":
        if not args.capture_id:
            parser.error("capture arm needs --capture-id")
        if args.apply_lag_s is None:
            if args.replica:
                parser.error("capture arm --replica needs --apply-lag-s (the replica's worst pull lag)")
            args.apply_lag_s = 0.0
    if args.command == "inventory" and args.action == "roster" and not args.manifest:
        parser.error("inventory roster needs --manifest")
    try:
        return args.run(args)
    except (journal.JournalError, OSError, ValueError, KeyError) as exc:
        print(f"kanban_fairness: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
