# In-process links display cutover

`hermes serve`/dashboard app startup selects the in-process links projection for
its exact launch origin scope. `HERMES_FACTS_LINKS` is ON when unset. Set it to
`0`, `off`, `false`, or `no` in the service launch environment for rollback on the
next natural startup; rollback closes any retained replacement before calling
`ScopeRefresher.resume()`. No live process is signalled by the cutover.

Startup fences and drains the scope's legacy refresher before registering one
external `RemoteLinksSource` on an in-process ChangeFeed/FactStore. Display
`stale_ok=True` entrypoints select that binding by the complete existing origin
scope (home, profile, board root, hub root, mail configuration). Reads take an
immutable generation under the FactStore publication condition and return
separate mutable copies. Strict/read-your-writes callers are unchanged. Explicit
request bindings still take precedence; other profile/storage scopes are not
implicitly authorized and keep their existing path.

The source captures the same canonical origin-ledger/legacy projection under
`_links_lock`, with the lock wait charged against its five-second capture budget.
It recaptures external input every 30 seconds after completion. Failed captures
publish unknown/stale, never a successful empty inventory; display requests do
not fall back to synchronous projection or legacy refresher admission. Existing
90-second validity is checked on every display read. A dead feed is unavailable.
No fact-host socket, replica IPC, demand channel, or per-request watch is started.

Shutdown closes the feed and its captured worker before removing the binding and
resuming legacy admission. Failed cleanup retains the startup owner and fence,
blocking a replacement until cleanup succeeds. The drain itself retains its
fence on timeout; startup unwind explicitly rolls back only its own owner.

Verification covers actual ASGI composition, zero legacy admissions under repeated
ON reads, OFF and strict behavior, isolation, publication copy safety, failed
cleanup and drain timeout, and native origin-ledger projection parity. Production
CPU acceptance is separate: after natural deployment, identify the routed serve
and take a continuous >=10 minute native per-thread context-switch sample;
`kanban-remote-l` must remain below 2k context switches/second. A passing unit suite
or a merged PR is not that live result.
