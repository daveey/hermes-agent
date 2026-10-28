// Format an api-request failure for desktop.log. The renderer only ever sees
// the invoke rejection; the real stack lives here in main, so persist it
// before rethrowing. Clamp: paths and error detail must not bloat the log.
//
// [daveey] The line names the backend the request went to: `conn=<registry id>` for a request pinned to
// a registered gateway connection, else `conn=primary` (the legacy/primary route), plus `profile=` when
// the request names one. hub-sync asks every connection for /status, so a remote tunnel timing out
// (mac-mini down, a hub tunnel after sleep) used to read as "the local backend stalled" and sent a
// whole investigation after the wrong process (2026-10-28). The path still closes the bracket, so
// parsers keyed on `[hermes:api METHOD PATH]` (coach backend_timeouts) keep working.
const ID_MAX = 120

function clampToken(value: unknown): string {
  return String(value ?? '')
    .trim()
    .replace(/\s+/g, '_')
    .slice(0, ID_MAX)
}

export function formatApiRequestFailure(
  request: { connectionId?: unknown; method?: string; path?: string; profile?: unknown } | null | undefined,
  error: unknown
): string {
  const method = String(request?.method ?? 'GET').toUpperCase()
  const path = String(request?.path ?? '(no path)').slice(0, 500)
  const conn = clampToken(request?.connectionId) || 'primary'
  const profile = clampToken(request?.profile)
  const detail = error instanceof Error ? (error.stack ?? error.message) : String(error)
  const route = `conn=${conn}${profile ? ` profile=${profile}` : ''}`

  return `[hermes:api ${method} ${path}] ${route} ${detail}`.slice(0, 6000)
}
