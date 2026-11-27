# Cooperative home-transfer preflight

The explicit `hermes_cli.kanban_home_cli` source owner must negotiate the serving
Board before committing a new local preparation. This is dormant administrative
maintenance, not automatic migration, permission to migrate, or a legacy rehome
fallback.

The source sends an authenticated, read-only
`GET /v2/boards/{url-encoded-board}/home-transfer/capability` outside the local
writer. Protocol v1 requires a JSON object with integer `home_transfer: 1`.
Boolean, string, missing or unsupported versions do not qualify. The response is
bounded to 8192 bytes and is not cached across calls. The companion Board must
serve this capability only when its matching transfer and durable abort protocol
are available. An older Board without this endpoint is intentionally unsupported;
no local task, schema, preparation or outbox write occurs on preflight failure.
This core change does not implement, deploy or activate the companion Board route.

After successful preflight the native writer still rechecks source eligibility,
version, drainage and immutable tuple custody. The local fence must be committed
before the first transfer POST. A capability is readiness evidence, not a commit
or NONCOMMIT receipt: failures after that preparation (including HTTP 404 after a
server rollback, timeout or lost reply) retain the exact frozen attempt. They must
not clear its marker, change nonce, or infer noncommit from network errors.
Recovery still requires the Board's authenticated exact-tuple abort decision.
A later failed capability probe must likewise leave existing custody untouched.

Regression proof: `tests/hermes_cli/test_home_owner_preflight.py` uses real Hub
HTTP transport and a loopback Board. It checks unavailable/malformed capability
no-write refusal, continued ordinary local writes, native committed preparation
visible from a second connection before POST, exact retries, capability rollback
between calls, and unchanged custody after post-preparation errors.
