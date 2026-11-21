# Worker drain baseline and fixed observer

The legacy `worker_drain_census.census()` remains a positive lower bound and
never returns zero. The manifest census can return zero only when host processes,
work leases, and local results independently prove zero.

## Installation order

1. Install/activate the admission fence for the worker account.
2. Supply a **root-authored image manifest** (version 1), not an observed process
   allowlist. Each `units` entry declares `name`, `kind` (`infrastructure`,
   `hermes`, or `init`), `exe_sha256` and `fragment_sha256`. User units also
   declare `user`. PID1 uses `name: init.scope`, `package_path` and
   `package_sha256` instead of a fragment digest. Digests come from the reviewed
   image/package, never from a census of a busy host.
3. Its `runtime` declares absolute `core_root`, all reviewed `files` SHA256s
   (at least `worker_drain_capability.REQUIRED_FILES`), `capabilities`
   (`turn`, `claim`, `cron`, `plugin`), and absolute Hermes `homes`.
4. As root, execute the installed runtime module
   `hermes_cli.worker_drain_baseline_cli prepare IMAGE_MANIFEST` **before** the
   daemon starts. This verifies authored hashes and writes same-boot install and
   activation ticks. It does not learn or replace the image's hashes.
5. Start the daemon normally. A pre-install/pre-activation, warm or different-core
   daemon cannot attest. No message-ingress adapter may be configured or live,
   including secondary profiles. The running gateway inspects both its effective
   config and its adapter maps after secondary-profile initialization. A separate
   kernel-sealed inventory receipt is required in addition to its cold capability.
   Missing/unreadable inventory or config, a changed config/.env/legacy config,
   or subsequent adapter construction disables exemption. Construction revokes
   exemption **before** any adapter code executes; disabling it later cannot
   re-enable exemption in the same process.
6. Execute `hermes_cli.worker_drain_baseline_cli capture` as a root standalone
   oneshot when the host naturally becomes idle. Undeclared processes (including
   shells and helper parents) cause refusal. Never stop busy jobs to force a
   capture. Only exact declared MainPID/PID1 identities may enter the baseline.

The writer pins trusted root-owned non-writable parent directories, rejects
symlink ancestors, writes root-owned mode 0644 atomically and fsyncs the file and
directory. Failed capture invalidates the previous baseline. Every read checks
boot/IID/node epoch, PID/start/UID/cgroup/exe, unit/package/runtime digests and live
sealed descriptors. A reboot invalidates the old baseline: repeat prepare,
normal cold daemon startup and capture. Never rebind an old baseline to a new boot.

## Public observation API

`observe_all()` returns exactly `host_processes`, `work_leases`, `local_results`,
each an `Observation(state, count, reason)`. States are `zero`, `positive`,
`unknown`. `manifest_count()` returns a positive lower bound with precedence over
unknown, `0` only when all adapters prove zero, otherwise `None`.
`complete_count()` additionally applies the heartbeat persistent-jobs gate.
These in-process APIs do **not** exempt the calling daemon from the host census.
Root-run lease/result observation selects the activated account's inventory,
not root's unrelated empty Hermes home. Missing registered result DBs remain
UNKNOWN; the observer does not retire or delete their records.

## Fixed privileged observer/request binding

The state launcher owns privileged transport; the core observer executes no
payload and imports no Board client or token broker. After performing any requested
seal operation, the launcher must **exec**, not spawn beneath a surviving shell,
the installed runtime's `hermes_cli.worker_drain_observer` module as root. Pass one
base64-encoded UTF-8 JSON object with **exactly** these keys:

```json
{"version":1,"op":"census","iid":"i-12345678","boot":"LIVE_BOOT_ID","node_epoch":"LIVE_EPOCH","generation":3,"attempt":"LIVE_ATTEMPT","phase":"sealed"}
```

Use actual values from the live latch. No arbitrary command, core path, account,
proc root, observer PID or exemption list is accepted on this command line.
The fixed entry point alone may exclude its own census-only process. Calling its
`main()` as an imported library is refused. Other user processes, including the
launcher's parent, remain visible.

The observer revalidates activation/live identity and the exact latch binding
before and after observation. It returns a JSON receipt with `request_hash`
(canonical `worker_drain_fence.request_hash(request)`), `binding`, `adapters`,
`count`, `complete`. Here `complete` is deliberately stronger than a lower bound:
it is true only for an all-ZERO manifest. Unreadable adapters remain UNKNOWN.
Refusal exits 75 with `complete:false,count:null`; no Board credentials or RPC are
needed, including after worker credential revocation. The transport must reject a
receipt whose binding/hash differ from its request. A zero receipt alone does not
authorize termination: the scaler still needs its sealed-generation/attempt and
final-proof gates.

## Rollout state

Core producer/observer support does not mean a production baseline is installed.
The state installer/launcher owns invocation and privileged transport integration.
The real-worker installed complete idle ZERO observation remains a rollout gate,
not something unit tests or an image manifest can substitute for.
