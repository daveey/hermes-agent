// A turn the owner admitted but could not start because another writer held the chat's turn lease
// (agent/turn_facade_lease.py stamps `failure_reason: session_busy`; its lease-timeout result names
// `error: session_turn_lease_timeout:<id>`). The user's message never reached the model.

/** The backend's marker for a turn refused because another writer holds the session. */
export const SESSION_BUSY_FAILURE = 'session_busy'

const LEASE_TIMEOUT_ERROR_PREFIX = 'session_turn_lease_timeout:'

export interface RefusalSignals {
  error?: unknown
  failureReason?: unknown
  surfaceCode?: unknown
}

/** The surface code of a turn refused because its pinned route's subscription quota is out (agent/error_surface.py). */
export const ROUTE_REFUSED_CODE = 'route_refused'

/** Whether a failed turn never processed the user's message: a busy chat, or a route behind a quota wall. */
export function isSessionBusyRefusal({ error, failureReason, surfaceCode }: RefusalSignals): boolean {
  return (
    failureReason === SESSION_BUSY_FAILURE ||
    surfaceCode === SESSION_BUSY_FAILURE ||
    surfaceCode === ROUTE_REFUSED_CODE ||
    (typeof error === 'string' && error.trim().startsWith(LEASE_TIMEOUT_ERROR_PREFIX))
  )
}
