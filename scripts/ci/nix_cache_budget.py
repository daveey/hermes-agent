#!/usr/bin/env python3
"""Enforce the existing 5 GiB store budget without expanding repository quota."""

import json
import os
from pathlib import Path
import sys


def main():
    info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    # Nix 2.19 uses a list; current Nix keys records by store path.
    records = info.values() if isinstance(info, dict) else info
    size = sum(item["closureSize"] for item in records)
    report = f"Stable rooted closure: {size} bytes (limit: 5368709120 bytes)\n"
    print(report)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(report)
    if size > 5368709120:
        print("::error::Stable Nix roots exceed the 5 GiB store-cache budget")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
