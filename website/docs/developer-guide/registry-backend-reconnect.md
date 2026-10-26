---
title: Registry SSH backend reconnect
---

# Registry SSH backend reconnect

The Desktop preload exposes an explicit, generation-qualified reconnect for one
registry-owned SSH backend. This is not the profile-only `recycleBackend` API and
must not be replaced with a managed update or a generic remote process kill.

## Requirements

The running Desktop bundle must include the registry reconnect implementation
introduced in fork PR [51](https://github.com/daveey/hermes/pull/51). A checkout on
disk is not evidence that the running Electron main process includes it. Check
for both preload methods below; older bundles do not implement this API.

The already-running remote serve must support authenticated SSH ownership
protocol version 1 and `/api/health/retirement` prepare/commit/cancel with the
backend-owned admission fence (fork commit
`98745df3b9cb10468b4e3018184ee3a030bf675d`). Missing capability, unknown idle state,
and active work fail closed. An old serve cannot gain this capability merely
because its checkout was updated.

Only Linux remote targets are supported. The resident must be an owned,
non-primary registry SSH backend with a spawn nonce and no pending bootstrap or
stop. Windows and Darwin are refused before retirement preparation. Linux owned
termination uses runtime-independent process identity checks; a deleted managed
Python runtime is deliberately not a reason to refuse ownership proof.

## Executable Desktop API

Use the owning Desktop renderer, not a shell on the remote gateway:

```javascript
const connections = window.hermesDesktop?.connections
if (!connections?.reconnectTarget || !connections?.reconnect) {
  throw new Error('This running Desktop does not support scoped reconnect')
}
const target = await connections.reconnectTarget('hermes-hub', 'default')
if (!target) throw new Error('No eligible owned registry backend')
// Retain this exact object for any explicitly authorized recovery retry.
const result = await connections.reconnect(target)
if (!result.ok) throw new Error('Reconnect refused; do not use a broader fallback')
console.log({ previousGeneration: target.generation, generation: result.generation })
```

`reconnectTarget(connectionId, profile)` returns
`{ connectionId, profile, generation } | null`. It captures an opaque resident
generation, not a PID and not a durable configuration version. It is discovery,
not an idle assertion. `reconnect(target)` returns `{ ok: boolean, generation?:
string }`, or rejects on transport/lifecycle failures. Success means an owned
replacement was published; it does not prove an application-specific RPC works.

Do not automatically loop on refusal or obtain a different generation to bypass
an unresolved attempt. An ambiguous commit retains the exact-resident fence.
An authorized retry with the same target reconciles it: a live owner must again
prove ownership and commit retirement, or a read-only Linux process check must
prove the captured owner has exited. Unknown/live process probes never authorize
a kill. Replaced identity or generation fails closed. After successful retirement
but failed replacement bootstrap, the fence is released so ordinary dialing can
retry startup; that failure is not reported as successful reconnect.

## Safety boundaries and evidence

The connection gate prevents configuration mutation and new bootstrap publication
during reconnect. Existing unrelated profile residents may continue dispatching.
The coordinator and pool-stop guard protect the selected resident from concurrent
teardown and generation replacement. The primary backend, gateway daemon, other
connections and other profiles are not termination targets.

The remote retirement endpoint checks work and freezes admission atomically using
the same backend lock that admits a turn. A prepared fence can expire or be
cancelled; a committed fence remains closed through termination. Reading an idle
status beforehand or polling a lease table is not an equivalent fence.

Regression coverage is in the Electron `registry-reconnect`,
`registry-reconnect-service`, `registry-retirement-http`,
`registry-stopped-owner`, and `registry-reconnect-wiring` suites, plus pool-stop
and bootstrap suites. `tests/hermes_cli/test_backend_retirement.py` exercises the
authenticated endpoint and a real threaded race between admission and freezing.
The stopped-owner suite probes real local live/exited processes with synthetic
SSH transport. These are automated tests, not live reconnect evidence.

For rollout, record merged commit, released bundle, installed/running bundle,
remote capability, and live operation separately. The designated live operator
must perform only the approved target reconnect; a separate verification should
then check authenticated owner-routed inventory/detail/refresh and latency.
Never infer installation or live success from unit tests or a merged PR.
