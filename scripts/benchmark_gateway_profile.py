#!/usr/bin/env python3
"""Measure sampler CPU and baseline-subtracted process CPU on idle/active threads.

Run from checkout root: python3 scripts/benchmark_gateway_profile.py --seconds 10 --rounds 3
No running gateway is contacted, restarted or signalled. --cprofile instruments
only the sampler thread with a thread CPU timer; use separate uninstrumented
rounds for overhead comparisons.
"""
import argparse
import cProfile
import io
import json
from pathlib import Path
import platform
import pstats
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway.control_profile as profiler


def _wait_a(stop, interval):
    stop.wait(interval)


def _wait_b(stop, interval):
    stop.wait(interval)


def _worker(stop, barrier, interval):
    barrier.wait()
    if interval is None:
        stop.wait()
        return
    while not stop.is_set():
        _wait_a(stop, interval)
        _wait_b(stop, interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--threads', type=int, default=150)
    parser.add_argument('--hz', type=float, default=None,
                        help='Sampling frequency (default: omit hz, i.e. the control verb default)')
    parser.add_argument('--active-threads', type=int, default=0,
                        help='Workers alternating real helper stacks (remaining workers idle)')
    parser.add_argument('--active-interval', type=float, default=0.037,
                        help='Seconds in each alternating wait (avoid cadence aliasing)')
    parser.add_argument('--cprofile', action='store_true', help='Include sampler CPU hot-path stats')
    args = parser.parse_args()
    if not 0 <= args.active_threads <= args.threads or args.rounds < 1 or args.active_interval <= 0:
        parser.error('require 0 <= active-threads <= threads, rounds >= 1 and active-interval > 0')
    # Omitting hz takes the same default path as a live request without --hz.
    request = {'seconds': args.seconds} if args.hz is None else {'seconds': args.seconds, 'hz': args.hz}
    profiler.validate_params(request)
    stop = threading.Event()
    barrier = threading.Barrier(args.threads + 1)
    workers = [threading.Thread(target=_worker,
                               args=(stop, barrier, args.active_interval if i < args.active_threads else None),
                               name=f'{"active" if i < args.active_threads else "idle"}-{i}')
               for i in range(args.threads)]
    collect = profiler._collect
    sampler_measurements = {}

    def measured_collect(*params):
        sampler_measurements.clear()
        sampler_measurements['instrumented'] = args.cprofile
        stats = cProfile.Profile(timer=time.thread_time) if args.cprofile else None
        cpu, wall = time.thread_time(), time.monotonic()
        if stats is not None:
            stats.enable()
        try:
            return collect(*params)
        finally:
            if stats is not None:
                stats.disable()
            cpu, wall = time.thread_time() - cpu, time.monotonic() - wall
            sampler_measurements.update(cpu_s=cpu, wall_s=wall, core_percent=100 * cpu / wall)
            if stats is not None:
                text = io.StringIO()
                report = pstats.Stats(stats, stream=text)
                report.sort_stats('tottime').print_stats(30)
                sampler_measurements['cprofile_thread_cpu'] = text.getvalue()
                # Some CPython monitoring implementations observe other threads
                # too. A thread_time timer is invalid across those threads; expose
                # that limitation rather than passing off corrupted times as CPU.
                contaminated = any(getattr(entry.code, 'co_name', '') in ('_worker', '_wait_a', '_wait_b')
                                   or entry.inlinetime < 0 for entry in stats.getstats())
                sampler_measurements['cprofile_valid'] = not contaminated
                if contaminated:
                    sampler_measurements['cprofile_warning'] = (
                        'Cross-thread events/negative times detected: cProfile timings are invalid on this interpreter. '
                        'Use the uninstrumented sampler CPU measurements for overhead.')

    profiler._collect = measured_collect
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
                payload = profiler.profile(request) if mode == 'profile' else None
                if mode == 'baseline':
                    stop.wait(args.seconds)
                cpu, wall = time.process_time() - cpu, time.monotonic() - wall
                measurements[mode] = dict(cpu_s=cpu, wall_s=wall, core_percent=100 * cpu / wall)
                if payload:
                    measurements[mode]['hz'] = payload['hz']
                    measurements[mode]['samples'] = payload['samples']
                    measurements[mode]['workers_seen'] = sum(t['name'].startswith(('idle-', 'active-')) for t in payload['threads'])
                    active = [t for t in payload['threads'] if t['name'].startswith('active-')]
                    measurements[mode]['active_workers_with_both_helpers'] = sum(
                        all(any(key.endswith(':' + name) for key, _ in t['top_inclusive'])
                            for name in ('_wait_a', '_wait_b')) for t in active)
                    measurements[mode]['sampler'] = dict(sampler_measurements)
            measurements['incremental_core_percent'] = measurements['profile']['core_percent'] - measurements['baseline']['core_percent']
            results.append(measurements)
    finally:
        stop.set()
        for worker in workers:
            worker.join()
        profiler._collect = collect
    print(json.dumps(dict(platform=platform.platform(), python=sys.version, params=vars(args), request=request, rounds=results,
                          median_incremental_core_percent=statistics.median(r['incremental_core_percent'] for r in results),
                          median_sampler_core_percent=statistics.median(r['profile']['sampler']['core_percent'] for r in results)), indent=2))


if __name__ == '__main__':
    main()
