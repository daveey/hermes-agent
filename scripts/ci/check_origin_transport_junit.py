#!/usr/bin/env python3
"""Fail the origin-transport lane unless every collected case ran and passed.

tests/origin_transport/ skips without ``--courier-client``; a green run over skipped or zero cases
would be the silent gap this lane exists to close (.github/workflows/origin-transport.yml).
Decided from pytest's JUnit counts, not its progress line.

    python3 scripts/ci/check_origin_transport_junit.py JUNIT.xml
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def verdict(xml_text: str) -> tuple[bool, str]:
    root = ET.fromstring(xml_text)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "skipped", "failures", "errors")}
    if counts["tests"] == 0:
        return False, "no test ran (collection selected nothing)"
    bad = {k: v for k, v in counts.items() if k != "tests" and v}
    if bad:
        return False, ", ".join(f"{v} {k}" for k, v in bad.items()) + f" of {counts['tests']}"
    return True, f"{counts['tests']} passed"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8-sig") as fh:
        ok, reason = verdict(fh.read())
    if ok:
        print(f"origin transport: {reason}")
        return 0
    print(f"::error::origin transport: {reason}; a skip is not a pass")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
