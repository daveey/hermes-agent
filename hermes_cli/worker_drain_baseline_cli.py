"""Root installation boundary for the image-authored drain manifest.

prepare must run after activation and before starting the daemon. capture runs
later as a standalone oneshot on the idle host; neither operation learns an
allowlist from /proc. Reboot requires prepare + daemon restart + capture again.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

from hermes_cli import worker_drain_baseline as baseline
from hermes_cli import worker_drain_fence as fence


def boot_ticks() -> int:
    # /proc/PID/stat starttime uses time since boot, including suspend. Round up
    # so processes created in the same scheduler tick cannot claim a cold start.
    return (time.clock_gettime_ns(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK')
            + 999_999_999) // 1_000_000_000


def prepare(image_manifest) -> dict:
    if os.geteuid() != 0:  # windows-footgun: ok -- Linux root-only image installer
        raise baseline.CaptureRefused('manifest preparation requires root')
    act = fence.load_activation()
    if act is None:
        raise baseline.CaptureRefused('activate fence before preparing manifest')
    ident = fence.current_identity(act)
    doc = copy.deepcopy(baseline.load_manifest(image_manifest))
    try:
        runtime = doc['runtime']
        tick = boot_ticks()
        runtime.update(installed_boot_id=ident.boot, installed_start_ticks=tick, activation_start_ticks=tick)
    except (KeyError, TypeError) as exc:
        raise baseline.CaptureRefused('image manifest needs a reviewed runtime contract') from exc
    from hermes_cli.worker_drain_capability import verified_contract
    verified_contract(doc)  # Verify the authored hashes; never replace them with observed hashes.
    baseline.write_baseline(baseline.MANIFEST_PATH, doc)
    return doc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('prepare', help='install image-authored hashes before daemon startup')
    setup.add_argument('image_manifest')
    commands.add_parser('capture', help='capture only declared, idle process identities')
    args = parser.parse_args(argv)
    try:
        doc = prepare(args.image_manifest) if args.command == 'prepare' else baseline.install()
    except (OSError, ValueError, baseline.CaptureRefused, fence.DrainRefused) as exc:
        print(f'drain baseline refused: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(doc, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
