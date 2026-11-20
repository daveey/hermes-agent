#!/usr/bin/env python3
"""Report cache effectiveness from tee'd Nix output, even on build failure."""

import os
from pathlib import Path
import re
import sys


def main():
    lines = []
    for name in sys.argv[1:]:
        path = Path(name)
        if path.exists():
            lines.extend(path.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    copies = sum(bool(re.match(r"^copying path '/nix/store/[^']+' from '", line)) for line in lines)
    fetches = sum(bool(re.match(r"^[^\s>]+> trying https://", line)) for line in lines)
    report = f"Paths copied from substituters: {copies}\n\nHTTPS source fetches: {fetches}\n"
    print(report)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write("## Nix cache effectiveness\n\n" + report)
    if (os.environ.get("GITHUB_EVENT_NAME") == "push"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("NIX_CACHE_HIT") == "true" and copies > 10):
        print(f"::warning::Nix primary-key hit still copied {copies} paths (budget: 10)")


if __name__ == "__main__":
    main()
