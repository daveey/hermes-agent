# Gateway and unloaded notification source scheduling

The gateway notifier, gateway remote FIFO owner and unloaded host executor use
`notification_loaded_f.Scheduler`'s existing process lifetime owner and **one
30-second reconciliation clock**. This is an in-process native wake cutover,
not activation of `InProcessFacts` or the dedicated `fact_host` transport.

`HERMES_FACTS_GATEWAY` defaults on. Setting it to `0`, `false`, `no` or `off`
withdraws these native subscriptions at the next executor iteration and restores
legacy admission. It does not change `display.loaded_notification_f`, restart a
process, start a second delivery executor, reset a notification cursor or release
an in-flight delivery. Pending action deadlines/refusal floors survive rollback.
This narrowly scoped emergency switch is not a secrets setting.

For no-restart control, the registered `display.gateway_notification_f` key defaults
to true and is ANDed with that environment switch. Its one native config lease is
separate from data invalidations: an unrelated config write does not wake collectors.
`hermes config set display.gateway_notification_f false` on the running profile
withdraws data watches and restores legacy cadence on the next executor iteration.
The config lease remains while executors are registered, so setting true rearms
the sources and catches up from the same durable state. Only the shared owner reads
config, on a native control hint, failed-read retry or the existing 30s census.
Unsupported native platforms and managed/nonlocal config updates use that census.
Unknown policy fails to legacy with a one-second shared retry, not native admission.
No environment mutation RPC or serve signal is used. A service drop-in is only a
future-start override; daemon-reload does not alter the current process environment.

## Sources and readiness

The shared owner captures the canonical board inventory and profile-store paths,
plus the hub replica and remote notice ledger. Existing Linux native directory
watches cover each DB and its WAL/SHM/journal, replacement and missing ancestors.
They are armed before the first healthy catch-up notification. Hints only wake
existing serialized executors; the canonical collectors, claim CAS, transcript
keys, FIFO custody and durable ACK paths remain the authorities.

The owner retains the registration context for scoped inventories. Roster changes
and missed native hints are repaired by the same 30-second census, not by a new
per-chat or per-subscriber source poller. Inventory/watch failures are explicitly
not ready and get a shared one-second retry; the existing executor can still do
conservative recovery work. Unsupported native platforms are census-only, with
no repeated one-second unsupported-backend retry. Readiness is logged as
`gateway notification native sources ready`; that is source-watch readiness,
not certification of a complete generic projection or upstream freshness.

## Timing boundaries

- Native DB commit hints can admit work before the census; callbacks do no delivery.
- The shared 30-second fallback bounds missed-hint/roster detection, **not**
  under-one-second upstream truth. Original remote freshness/expiry checks remain.
- Pending actions retain their collector-provided monotonic deadline even when
  the same empty result is incomplete. ACK/release and refusal retries retain
  the same executor and their existing pacing floor.
- Unloaded lease beats, role transfer and retirement remain independent of source
  quietness. Other host-pass inputs still receive the shared maintenance census;
  they are not claimed to have native source coverage here.
- The loaded chat path retains its own keyed hub batches and canonical serializers;
  f2 does not turn task-state hints into a durable event history.

Natural gateway/serve boot is required for installed activation. Do not signal a
serve process or force an update to obtain proof. Combined multi-consumer runtime,
RSS, painted latency and restart measurements are separate acceptance work.
