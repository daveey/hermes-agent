// [daveey] The renderer's one "the user used this remote profile" signal.
//
// Desktop main keeps a bounded remote warm set per SSH gateway: default plus the profiles used in the last 24 h
// (prewarmed at startup, cap 4), each also held by a 30-min keep-alive. Main used to count every non-passive dial
// as use, so a post-reconnect sweep (every pooled secondary socket redialing at once, roster and session-list
// reads) stamped every listed profile and decided the cap-4 set by tie order. Now dials never count. Only user
// actions on a registry route do: opening a session, a new chat, a send, an activation the user chose. Each of
// those calls noteRemoteUse, which main validates (SSH connection, real profile name) before stamping.

const NOTE_USE_THROTTLE_MS = 60_000

const lastNoted = new Map<string, number>()

/** Report a user action on (connectionId, profile). No-op for a bare/legacy route; at most once a minute per scope. */
export function noteRemoteUse(connectionId: null | string | undefined, profile: null | string | undefined): void {
  const id = String(connectionId ?? '').trim()
  const name = String(profile ?? '').trim() || 'default'
  const report = typeof window === 'undefined' ? undefined : window.hermesDesktop?.noteRemoteUse

  if (!id || typeof report !== 'function') {
    return
  }

  const key = `${id}\u0000${name}`
  const now = Date.now()

  if (now - (lastNoted.get(key) ?? -Infinity) < NOTE_USE_THROTTLE_MS) {
    return
  }

  lastNoted.set(key, now)

  // Best effort: an older main without the handler (or a failed IPC) must never fail the user's action.
  try {
    void Promise.resolve(report({ connectionId: id, profile: name })).catch(() => undefined)
  } catch {
    void 0
  }
}

export function _resetRemoteUseForTests(): void {
  lastNoted.clear()
}
