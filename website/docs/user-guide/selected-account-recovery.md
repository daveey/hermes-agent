# Selected OAuth credential recovery

`hermes auth reauth PROVIDER CREDENTIAL_ID` is an interactive recovery command for an exact locally owned credential-pool ID. Unlike `auth add`, it replaces that row instead of appending a second account. It accepts `anthropic` and `openai-codex`; labels and list indexes are not accepted as identifiers.

This initial command supports Anthropic `manual:hermes_pkce` and Codex `manual:device_code` / `manual:loopback_pkce` sources only. Borrowed credentials, environment tokens, singleton `device_code` rows and external credential stores are refused rather than copied into the current profile. It does not implement Desktop recovery launch or gateway routing.

The selected row must still exist, be enabled, and be unchanged when login finishes. Its ID, label, priority and unrelated metadata are preserved. Login runs without holding the auth-store lock; a concurrent deletion or edit prevents replacement. A Codex login must match both the stored account/workspace and subject. Legacy Anthropic grants lack a verifiable principal, so the terminal requires explicit replacement confirmation and reports identity as unverified, never as a confirmed same-account reconnect.

Anthropic authorization-code input is masked; if terminal echo cannot be disabled, recovery stops rather than reading an unmasked code. Do not run this in an agent-readable or recorded terminal. A future Desktop recovery surface must provide a private terminal and bind the owning gateway and profile explicitly; this command alone does not provide that transport.

The completion message confirms credential persistence, not provider health. Refresh and verify account health separately. If interrupted during persistence, the command reports an unverified outcome rather than claiming no change occurred.

## Dashboard device sign-in for a selected Codex credential

`POST /api/providers/oauth/openai-codex/start` accepts an optional JSON body `{"credential_id": "<pool id>"}`. The session is then bound to that exact owned row before any provider call, and its response carries `target_bound: true`. On approval the grant is committed into the selected row with the same checks as the command above (unchanged row, same account/workspace and subject, enabled), instead of the singleton login that would add a second row. The poll response reports `credential_id` and `identity_verified` on success, or `status: error` with `reason: reauth_conflict` and no write. A malformed id, an unsupported provider or an unreplaceable row is refused (400/409); a named target never degrades to the ordinary untargeted login. Cancelling the session before commit leaves the row untouched. Callers on an older core receive no `target_bound` field and must cancel that session rather than let it complete as an account-add.
