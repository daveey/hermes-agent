/** A readiness deadline is an observation, not a broken installation. */
export const STARTUP_DEADLINE = 'startup_deadline' as const

export type StartupDeadline = { ok: false; reason: typeof STARTUP_DEADLINE; message: string }
export type StartupConnectionResult<T> = { ok: true; connection: T } | StartupDeadline

export function backendReadinessTimeout(message: string): Error & { code: typeof STARTUP_DEADLINE } {
  return Object.assign(new Error(message), { code: STARTUP_DEADLINE })
}

export function isBackendReadinessTimeout(error: unknown, stage = 'resolving'): boolean {
  return stage === 'resolving' && typeof error === 'object' && error !== null
    && 'code' in error && error.code === STARTUP_DEADLINE
}

/** Return deadline data instead of throwing across Electron IPC (which loses Error properties). */
export async function captureStartupDeadline<T>(connection: Promise<T>): Promise<T | StartupDeadline> {
  try {
    return await connection
  } catch (error) {
    if (!isBackendReadinessTimeout(error)) { throw error }

    return { ok: false, reason: STARTUP_DEADLINE, message: (error as Error).message }
  }
}

/** Runs in the renderer, AFTER contextBridge has copied the plain result. */
export function unwrapStartupConnection<T>(result: StartupConnectionResult<T>): T {
  if (isStartupDeadlineResult(result)) { throw backendReadinessTimeout(result.message) }
  if (result.ok !== true) { throw new Error('Invalid startup connection result') }

  return result.connection
}

/** Legacy callers still receive a descriptor or an ordinary rejected Error. */
export function unwrapConnectionDeadline<T>(result: T | StartupDeadline): T {
  if (isStartupDeadlineResult(result)) { throw new Error(result.message) }

  return result as T
}

export function isStartupDeadlineResult(result: unknown): result is StartupDeadline {
  return typeof result === 'object' && result !== null && 'ok' in result && result.ok === false
    && 'reason' in result && result.reason === STARTUP_DEADLINE && 'message' in result && typeof result.message === 'string'
}
