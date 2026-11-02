#!/usr/bin/env python3
"""Reproduce profiler overhead: process CPU, same idle threads, baseline subtraction.

Run from checkout root: python3 scripts/benchmark_gateway_profile.py --seconds 10 --rounds 3
No running gateway is contacted, restarted or signalled.
"""
import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.control_profile import profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--threads', type=int, default=150)
    parser.add_argument('--hz', type=float, default=100)
    args = parser.parse_args()
    stop = threading.Event()
    barrier = threading.Barrier(args.threads + 1)
    def idle():
        barrier.wait()
        stop.wait()
    workers = [threading.Thread(target=idle, name=f'idle-{i}') for i in range(args.threads)]
    for worker in workers:
        worker.start()
    barrier.wait()
    results = []
    try:
        for index in range(args.rounds):
            measurements = {}
            # Alternate order to expose warmup/drift instead of hiding it.
            order = ('baseline', 'profile') if index % 2 == 0 else ('profile', 'baseline')
            for mode in order:
                wall, cpu = time.monotonic(), time.process_time()
                payload = profile({'seconds': args.seconds, 'hz': args.hz}) if mode == 'profile' else None
                if mode == 'baseline':
                    stop.wait(args.seconds)
                cpu, wall = time.process_time() - cpu, time.monotonic() - wall
                measurements[mode] = dict(cpu_s=cpu, wall_s=wall, core_percent=100 * cpu / wall)
                if payload:
                    measurements[mode]['samples'] = payload['samples']
                    measurements[mode]['idle_threads_seen'] = sum(t['name'].startswith('idle-') for t in payload['threads'])
            measurements['incremental_core_percent'] = measurements['profile']['core_percent'] - measurements['baseline']['core_percent']
            results.append(measurements)
    finally:
        stop.set()
        for worker in workers:
            worker.join()
    print(json.dumps(dict(platform=platform.platform(), python=sys.version, params=vars(args), rounds=results,
                          median_incremental_core_percent=statistics.median(r['incremental_core_percent'] for r in results)), indent=2))


if __name__ == '__main__':
    main()
