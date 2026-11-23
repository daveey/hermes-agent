# Session Tasks loader-owned identity (offline preparation)

This additive SDK API is a **provenance association**, not renderer security confinement,
mutable closure-state attestation, backend qualification, or installed-app evidence.
The companion is not an authority for its own path, commit, digest, or receipt.

## Source-operation / observation map

| Returned field | Core-owned producer / observation |
|---|---|
| `schemaVersion` | `readSessionTasksLoadedIdentity`, literal `1` |
| `status`, `reason` | Private activation state; successful register settlement, dependency evidence and exact current-surface comparison; otherwise `NOT_READY` |
| `pluginId` | Actual imported module's default export, validated by the existing production loader |
| `source.origin`, `source.path` | Existing loader origin and disk-file record; plugin binding accepts neither. Disk discovery supplies its source path. `null` means no disk-file association, not a guessed path. This is not an additional filesystem-realpath proof. |
| `original` | SHA-256 and length of `TextEncoder` UTF-8 bytes of the exact source argument imported by the loader |
| `rewritten` | Same hash operation on the exact rewritten string passed to `Blob`; import rewrite mapping retained with that activation |
| `activationGeneration` | Monotonic private counter at activation construction; no caller override |
| `realm` | Core-generated per-module-realm UUID; not an operating-system/process attestation |
| `navigation` | `location.href` captured at activation; document reference held privately; read also compares both, while pagehide/beforeunload/popstate/hashchange retire synchronously |
| `dependencies[].specifier` | Static import/re-export specifier scan over actual imported source; closed supported set: SDK, React, JSX runtime. Dynamic literal/computed/comment-separated imports are unqualified. |
| `handle`, `shimUrl` | Core cached shim map and evaluation capture; opaque handle is correlation only, never an authority or namespace reference |
| `shim` | Hash/length of exact original shim source retained when its Blob is created |
| `servedAssets` | Actual fetched renderer manifest plus recursively visited **static** JS closure containing `sdk/runtime.ts`; bytes hashed when the shim evaluates, never derived from a caller receipt. Unsupported/unbundled/failed fetches remain unqualified. |
| `currentSurfaceMatches` | Current own descriptor of `window.__sessionTasks`, exact object identity, own string/symbol names and descriptor flags/value/get/set **references** compared to the private bound descriptor map; no getter invocation |

The private activation also retains the actual imported module namespace, original and rewritten
strings, the mapping to shim imports, their imported shim namespaces and their captured export
namespace references. None of those references or source strings is serialized by the diagnostic.
Responses are detached by-value records: editing a returned digest cannot change registry custody.

The shim callback runs immediately after `const m = globalThis.<namespace>` in ESM evaluation.
Its promise for source/asset evidence begins there. Cached ESM shims retain their first evaluation
captures, even if globals are replaced before a later registration. Registration never reconstructs
references from globals. The temporary hook is scoped/refcounted and restores any prior descriptor.
This does **not** prevent arbitrary hostile code from tampering with a shared renderer or core internals.

`readServedAssets` uses a 5-second aggregate abort deadline and a 256-entry static graph limit.
Assets must be relative `assets/*.js` paths without traversal. Redirects/read failures reject evidence.
Lazy chunks, lazy transitive behavior, CSS semantics and mutable captured objects are outside scope.
The read is an observation of bytes served at shim evaluation, not a JavaScript-engine compiled-byte
receipt: later consumers must compare it against the independently reviewed, immutable/content-addressed
bundle inventory. A mutable URL serving different bytes is not qualified by its name alone.

## Binding and lifecycle

The companion calls `ctx.bindSessionTasksDiagnostic?.(surface)` immediately after assigning its surface.
Exactly one object binding is accepted, only from the single synchronous registering activation.
Duplicate, invalid, reentrant or late calls cannot qualify. Binding is pending until register settles;
async register rejection retires it. Old/absent core leaves the companion functional without qualification.
An uninstrumented plugin still loads normally but the diagnostic reports `MISSING_BINDING`.
Missing or rejected WebCrypto must not gate ordinary loading: the byte-identity boundary
returns empty digests with actual UTF-8 lengths and `DIAGNOSTIC_IDENTITY_UNAVAILABLE`.
The whole companion remains functional but cannot reach `MATCH` in that context; a later
consumer must reject those empty digests. This is fixture-tested compatibility, not a claim
about cryptography availability in any installed Electron build.

The loader reserves the plugin ID before awaiting byte identity. A different file is refused;
a newer same-file request supersedes the previous claim. Unload cancels a pending claim,
and stale completion/finally cleanup cannot publish or release its replacement's reservation.
Disable during an owned reload cancels that request; re-enable retains the last established
activation handle rather than adopting an incomplete module. Disk discovery/watch/refresh
keeps a per-entry request token from before the file read; retired/superseded reads,
imports and identity completions cannot commit source or inventory. The private evaluator
distinguishes cancellation from a failed module, while the public `loadRuntimePlugin`
return shape stays unchanged. Cancelled source bytes are not marked loaded, so later
refresh can retry them. The companion registers its
cleanup with `onDispose` before publishing and immediately binding the surface, so rejection
rolls back its actual listeners, poll/RPC timers, style and surface.

Unloading/disable invalidates **before** any disposer. Reload invalidates the prior owned-file evidence
before its first asynchronous evaluation/read. New activation identity is distinct. Late old-generation
rejection/cleanup cannot retire or delete a replacement surface. The companion's timer cleanup is idempotent
with respect to surface ownership, although it always clears its own timer.

## Later runner consumption contract (not implemented here)

1. Read only the supported `host.diagnostics.readSessionTasksLoadedIdentity` through trusted core SDK
   plumbing. Missing API, thrown read, unknown schema, absent fields or `NOT_READY` => stop, report
   `NOT_READY`. Never fall back to a companion/global receipt, exported source text, matching version,
   method names, function `toString`, a blob URL or a previously accepted JSON record.
2. Compare the complete identity to the independently reviewed exact source/build inventory: origin/path,
   original and rewritten digests/lengths, supported dependency specifiers, actual served manifest/static
   chunk digests and expected realm/navigation/generation. Handles only correlate this admission.
3. Just before **each** mutation, perform a second fresh identity read and current-controller re-admission.
   Require the same admitted realm/navigation/generation/dependency identities, `MATCH` and
   `currentSurfaceMatches`; reject replacement, pending, unload, navigation or any changed field.
   Do not capture a controller once and issue a batch based on a prior identity read.
4. Identity qualification is necessary, not sufficient: separately verify controller/backend links,
   operation-specific permissions, safety scope and current window authority. This slice grants none.

No runner code, native execution, scheduler or publication gate is changed by this preparation.
An older unsafe batched bridge is not rehabilitated by this diagnostic.

## Reproduction on an owned Linux fixture

Use the pinned lockfile and supported Node version; from `apps/desktop`:

```
HERMES_K01_COMPANION=/absolute/path/to/real/session-tasks/plugin.js \
  pnpm exec vitest run --maxWorkers=1 --no-file-parallelism \
  src/contrib/session-tasks-identity.test.ts \
  src/contrib/session-tasks-capture.test.ts src/sdk/loaded-assets.test.ts \
  src/contrib/k01-review-probes.test.ts
node scripts/k01-built-replay.mjs /absolute/path/to/real/plugin.js /owned/output
python3 scripts/k01-mutants.py /absolute/core /absolute/mods /owned/mutation-output
```

The integration imports/registers the whole real companion through the production loader and SDK.
Vitest mocks only served-asset IO. The separate built replay uses the actual Vite aliases/chunk grouping,
actual built loader/SDK, actual emitted manifest and static chunk bytes; its fixture transports file reads
and blob imports in an owned jsdom realm. This is not a full installed Desktop build or native UI test.
The dedicated process disposes the plugin, closes jsdom/timers and library MessagePorts; it never forces
success with `process.exit`. Every assertion must complete before its PASS receipt is written.

The mutation runner snapshots Git-selected input files to an isolated fixture and modifies only that
copy. Its dependency symlinks point to the explicitly supplied owned checkout, not a live install.
It retains exact input hashes, mutation definitions, commands, per-test JSON, stdout/stderr and mutant
hashes. Collection failure, missing reports and timeout are `INVALID`, not authority-guard kills.
