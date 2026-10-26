"""Explicit observation-only entry point; no worker, timer or apply integration.

Run: python -m hermes_cli.retention_cli observe --workspace /absolute/workspace
     --candidate exact/relative/cache [--candidate another/cache]

The output is NOT a live apply plan. Stdout redirection is the caller's choice;
this command never provisions retention state or writes inside a candidate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_cli.retention_plan import inventory, plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser("observe", help="retain-only bounded inventory")
    observe.add_argument("--workspace", type=Path, required=True)
    observe.add_argument("--candidate", action="append", required=True)
    observe.add_argument("--max-entries", type=int, default=100000)
    commands.add_parser("apply", help="unavailable: code-level safety gate")
    args = parser.parse_args(argv)
    if args.command == "apply":
        print(json.dumps({"outcome": "retained", "reason": "apply_not_enabled",
                          "deletion_authorized": False, "removed_bytes": 0}))
        return 2
    try:
        observations = [inventory(args.workspace, candidate, args.max_entries)
                        for candidate in args.candidate]
        report = plan(observations)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc), "deletion_authorized": False}))
        return 1
    report["inventories"] = observations
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report["scan_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
