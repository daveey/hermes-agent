# On-demand gateway profiling

`hermes gateway profile --seconds 20 --hz 100` samples the **running gateway process**, not the CLI process. Add `--json` for the wire result, or `--top 40` to set the number of self/inclusive frames per thread. It uses the existing local control socket and its filesystem/pipe authorization boundary. It does not restart the gateway, use signals, require root, or install dependencies.

The control verb is `profile`, with `params`:

- `seconds`: finite number, 1–60 inclusive, default 20.
- `hz`: finite number, 10–200 inclusive, default 100.
- `top`: positive integer, default 40.

Booleans, numeric strings, out-of-range numbers and non-finite numbers are rejected. Only one profile may run process-wide; another request fails immediately. A daemon sampler does the work while one of the four dedicated control workers waits. The CLI timeout is the requested duration plus 10 seconds. It reports a failed/unavailable request rather than silently starting another gateway.

## Result and interpretation

```json
{
  "seconds": 20,
  "hz": 100,
  "samples": 2000,
  "threads": [{
    "name": "example",
    "native_id": 123,
    "cpu_s": 0.01,
    "cpu_partial": false,
    "samples": 2000,
    "first_seen_sample": 0,
    "last_seen_sample": 1999,
    "top_self": [["/path/module.py:42:worker", 2000]],
    "top_inclusive": [["/path/module.py:42:worker", 2000]]
  }]
}
```

This is an illustrative schema, not a captured profile. Frame keys use `co_filename:co_firstlineno:co_name`; the line is the function/code definition line, **not** its currently executing line. First/last sample indexes are zero-based. Recursive appearances of the same frame key count only once per inclusive sample. No locals, source lines or argument values are read or returned. File names and thread names can themselves contain sensitive information; treat the output as local diagnostic data.

Stack counts are **wall samples**, not CPU attribution. Sleeping threads can accumulate many samples with almost zero CPU. CPU seconds are independently measured native thread CPU-clock deltas. Sampling is GIL-dependent and can be delayed by busy threads or native extensions; inspect the actual sample count. Threads without an identifiable Python `threading.Thread`/native ID are not reported. Purely native threads without Python frames are outside the stack census.

CPU starts at each thread's first observation, not at its lifetime origin. Exited threads remain in the result; their last readable CPU clock supplies a **partial/lower bound**, explicitly marked `cpu_partial: true`. Unavailable CPU is `null`, never a guessed zero. Work before the first observation and after the last readable observation cannot be recovered. A thread that starts and exits entirely between samples cannot be detected. Thread objects and native IDs distinguish lifetimes even when Python thread identifiers or names are reused.

Linux reads `clock_gettime((~native_id << 3) | 6)`, the kernel `MAKE_THREAD_CPUCLOCK(tid, CPUCLOCK_SCHED)` encoding. This avoids `pthread_getcpuclockid(pthread_t)`, whose userspace pthread pointer may already have been freed when a thread exits; the numeric kernel lookup fails safely for exited IDs. No arbitrary process is queried. macOS uses own-process `proc_pidinfo(PROC_PIDTHREADID64INFO = 15)` through ctypes and sums the user/system nanosecond counters of `struct proc_threadinfo` (112 bytes). On macOS `threading.native_id` is `pthread_threadid_np()`'s 64-bit kernel `thread_id`; XNU (`bsd/kern/proc_info.c`, `osfmk/kern/bsd_kern.c:fill_taskthreadinfo`) matches flavor 15 against `thread->thread_id` but `PROC_PIDTHREADINFO = 5` against `machine.cthread_self` (the TSD handle listed by `PROC_PIDLISTTHREADS`; on arm64 `pthread_self() + 224`). Flavor 5 therefore fails with `ESRCH` for every native ID. Kernel thread IDs are not reused, so an exited thread reads as unavailable and cannot alias a later thread. Other hosts return unavailable CPU.

To reduce idle sampling overhead, identical leaf frame objects reuse their previous stack only when their native CPU clock is unchanged. Code keys use identity rather than structural code-object hashing. Repeated stacks are aggregated before deriving self/inclusive counts. Only the preceding frame snapshot is retained; frame references are cleared at completion. No frame or code objects appear in the result.

Responses include the complete envelope and newline in the size budget and stay below the 512 KiB response cap. Oversized results omit whole frame entries or whole threads (including giant names), never rewrite frame keys or names. They add `truncated`, `omitted_threads`, and `omitted_frames`. The request ID is included in the budget. A response whose envelope alone is too large uses the existing `response_too_large` error; socket requests are separately limited to 64 KiB.

## Reproducible overhead measurement

From the checkout root:

```sh
python3 scripts/benchmark_gateway_profile.py --seconds 5 --rounds 3 --threads 150 --hz 100
```

This creates an isolated process with 150 idle event-waiting threads. Each round measures **whole-process** `time.process_time()` and monotonic wall time, once without profiling and once with it; order alternates. Incremental percent of one core is `(profile CPU / profile wall - baseline CPU / baseline wall) * 100`. Thread creation is outside the measured intervals. The script prints raw measurements, Python/OS metadata, samples and idle-thread census; it never contacts a live gateway.

Measured on Linux 7.0.0-1014-aws, x86_64, glibc 2.39, Python 3.14.9:

| Round | Baseline CPU s | Baseline wall s | Profile CPU s | Profile wall s | Incremental % core | Samples / idle threads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.002312311 | 5.000162899 | 0.108611255 | 5.001772645 | 2.12521 | 500 / 150 |
| 2 | 0.000146035 | 5.000233187 | 0.113924301 | 5.001637069 | 2.27482 | 500 / 150 |
| 3 | 0.000075944 | 5.000186206 | 0.109895571 | 5.001116799 | 2.19590 | 500 / 150 |

Median incremental overhead: **2.19590% of one core**, below the 3% target on this workload/host. The initial straightforward stack implementation measured 10.38331% median under the same parameters; the final result is not a baseline-free estimate. These numbers do not guarantee overhead for deep/changing stacks or more threads.

Measured on macOS 27.2 (Darwin 27.2.0, xnu-13432.41.4), Apple M1 (8 cores), Homebrew Python 3.12.2, with the same 150 idle threads, 100 Hz, 3 × 5 s rounds. This was an isolated stdlib probe equivalent to the benchmark script; the host was shared, with load average about 12:

| Round | Baseline CPU s | Baseline wall s | Profile CPU s | Profile wall s | Incremental % core | Samples / idle threads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.001373 | 5.000838 | 0.800702 | 5.007097 | 15.96388 | 500 / 150 |
| 2 | 0.000114 | 5.000553 | 0.825325 | 5.007088 | 16.48085 | 500 / 150 |
| 3 | 0.000571 | 5.007671 | 0.832967 | 5.016647 | 16.59266 | 500 / 150 |

Median incremental overhead on macOS: **16.48085% of one core, which misses the 3% target**. An attribution run (`_collect` only, same workload) gave three medians. The real reader measured 16.245%, a constant-CPU stub 6.315%, and a `None` stub (always a full stack walk) 10.95%. About 10 points therefore come from the 150 `proc_pidinfo` syscalls per tick. The Python sampling loop itself costs about 6% on this host. On Linux the same attribution measured 2.311 / 1.542 / 3.252%.

**PyDLL reader (current code).** `proc_pidinfo` is now loaded through `ctypes.PyDLL`, so the GIL stays held for this bounded, non-blocking per-thread read. Under `CDLL` the GIL was released and re-acquired on every call (150 per tick). The size constant is precomputed and the buffer is passed with `addressof`. A fresh struct is still allocated per call, so concurrent readers never share a buffer. Sample semantics are unchanged. The A/B ran on the Mac mini as interleaved `python3.12 -I probe.py <module> 5 3` subprocesses: 5 s, 3 rounds, 150 idle threads, 100 Hz. The host load average was about 45, so absolute figures are inflated. Every probe check passed for both readers. Medians:

| Run | CDLL median % (rounds) | PyDLL median % (rounds) |
|---|---|---|
| 1 | 16.68482 (16.19497, 16.68482, 16.75607) | 15.42431 (15.15525, 15.43350, 15.42431) |
| 2 | 16.30566 (16.30566, 16.85843, 16.13515) | 15.14392 (15.03389, 15.14392, 15.26198) |

PyDLL saves about 1.2 points of core, roughly 7%. **macOS still misses the 3% target** at about 15%. Linux after this change measured a **2.18334%** median (`scripts/benchmark_gateway_profile.py --seconds 5 --rounds 3 --threads 150 --hz 100`). Not yet evaluated: one Mach `task_threads` call plus `thread_info` per port (still one syscall per thread, plus port-to-thread-id mapping) and lowering per-thread CPU-read frequency (which changes sample semantics).

## Verification status

Tests are run only through `scripts/run_tests.sh`. The contract test was first corrected from an erroneous global self table/`cpu_seconds` shape to the approved per-thread shape, then failed on the base with `unknown verb: 'profile'`. Incremental RED→GREEN runs covered the sampler, argument validation, concurrent refusal, response trimming, CLI parser/dispatch and invalid parameter objects. Additional acceptance tests cover recursive inclusive deduplication, real sleeping/burning threads, short-lived threads and successive same-name thread lifetimes. The CLI integration test runs against a real temporary Unix socket, checks JSON and text, and requires identify/status each to answer in under 0.5 seconds while the profile is in progress.

Final Linux verification: **89 passed, 0 failed, 1 macOS-only skipped** across the four new profiler test files plus six related control-socket/diagnostics files. `python3 scripts/check --base HEAD` passed all **15 checks**. The full repository test suite was not run. After the macOS flavor fix, the four profiler test files gave **26 passed, 0 failed, 1 macOS-only skipped** via `scripts/run_tests.sh`. That count includes the new portable `test_darwin_reader_queries_thread_id64_flavor`, which fails on the flavor-5 code with `assert None == 1.75`.

**macOS native verification** ran as a disposable plain `python3.12 -I` subprocess on a Mac mini (macOS 27.2, arm64). It imported only the copied stdlib module, with no Hermes bootstrap and no gateway contacted, restarted or signalled. With the original flavor 5, every CPU read returned `None`: `proc_pidinfo` returned 0 bytes with errno 3 (ESRCH). Busy, native-GIL-released, sleeping and short-lived rows all had `cpu_s: null`. In the 150-thread run, all 150 rows were `null`. The repo tests `test_macos_cpu_reader_tracks_current_thread` and `test_darwin_reader_queries_thread_id64_flavor` both failed. With flavor 15, both tests passed. The counter delta divided by `time.thread_time()` was 1.0003, confirming nanosecond units. A 2 s, 100 Hz `_collect` measured a busy Python thread at 1.837 s, a `hashlib.sha256` (GIL-released) thread at 0.665 s, and a sleeping thread at 0.003 s. A short-lived thread measured 0.143 s with `cpu_partial: true`; after it exited, its native ID read `None`. All 150 idle threads had readable CPU. Those repo tests ran under a stdlib pytest shim (`approx`, `monkeypatch.setattr`), not real pytest, and the macOS overhead figure above is from a loaded shared host.
