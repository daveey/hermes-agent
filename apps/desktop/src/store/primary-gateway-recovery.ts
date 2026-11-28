import type { HermesGateway } from '@/hermes'
import { RECONNECT_ATTEMPT_TIMEOUT_MS } from '@/lib/with-timeout'

/** Only the boot hook redials; requests wait before their first send, never replay. */
export type PrimaryGatewayRecoverer = () => boolean

interface Owner {
  gateway: HermesGateway
  disposed: boolean
  recover: PrimaryGatewayRecoverer
  waiters: Set<(error: Error) => void>
}

let owner: Owner | null = null

function disposeOwner(entry: Owner) {
  entry.disposed = true

  if (owner === entry) {
    owner = null
  }

  for (const fail of [...entry.waiters]) {
    fail(new Error('Hermes gateway unavailable'))
  }
}

export function registerPrimaryGatewayRecovery(gateway: HermesGateway, recover: PrimaryGatewayRecoverer): () => void {
  if (owner) {
    disposeOwner(owner)
  }
  const entry: Owner = { gateway, disposed: false, recover, waiters: new Set() }
  owner = entry

  return () => disposeOwner(entry)
}

const abortError = () => new DOMException('Aborted', 'AbortError')

export interface PrimaryGatewayReady {
  /** Remaining caller deadline, undefined when none was supplied. */
  remainingMs: number | undefined
}

/** Wait on the exact registered primary. Never dial or change the foreground route. */
export function waitForPrimaryGateway(
  gateway: HermesGateway,
  timeoutMs: number | undefined,
  signal: AbortSignal | undefined
): Promise<null | PrimaryGatewayReady> {
  if (signal?.aborted) {
    return Promise.reject(abortError())
  }

  if (gateway.connectionState === 'open') {
    return Promise.resolve({ remainingMs: timeoutMs })
  }
  const entry = owner

  if (!entry || entry.disposed || entry.gateway !== gateway) {
    return Promise.resolve(null)
  }

  const startedAt = Date.now()
  const hasDeadline = timeoutMs !== undefined && timeoutMs > 0
  const budget = hasDeadline ? timeoutMs : RECONNECT_ATTEMPT_TIMEOUT_MS

  return new Promise((resolve, reject) => {
    let settled = false
    let offState: (() => void) | undefined

    const finish = (outcome: () => void) => {
      if (settled) {
        return
      }
      settled = true
      clearTimeout(timer)
      offState?.()
      signal?.removeEventListener('abort', onAbort)
      entry.waiters.delete(fail)
      outcome()
    }

    const fail = (error: Error) => finish(() => reject(error))
    const onAbort = () => fail(abortError())
    const timedOut = () =>
      fail(new Error(`Timed out waiting for the Hermes gateway to reconnect (${Math.round(budget / 1000)}s)`))

    const opened = () => {
      const elapsed = Date.now() - startedAt

      if (elapsed >= budget) {
        timedOut()

        return
      }

      finish(() => resolve({ remainingMs: hasDeadline ? timeoutMs - elapsed : timeoutMs }))
    }

    const timer = setTimeout(timedOut, budget)
    entry.waiters.add(fail)
    signal?.addEventListener('abort', onAbort, { once: true })
    offState = gateway.onState(state => {
      if (state === 'open') {
        opened()
      }
    })

    // onState immediately reports the current state. Its disposer is not yet
    // assigned during that callback, so unsubscribe again after assignment.
    if (settled) {
      offState()

      return
    }

    if (signal?.aborted) {
      onAbort()

      return
    }

    try {
      if (!entry.recover()) {
        finish(() => resolve(null))
      } else if (gateway.connectionState === 'open') {
        opened()
      }
    } catch (error) {
      finish(() => reject(error))
    }
  })
}

/**
 * Send on `gateway` after waiting for its owner's redial. `strict` (the
 * owner-pinned primary path) fails when no owner can redial a closed client;
 * otherwise the closed client rejects the send as before.
 */
export async function requestAfterPrimaryRecovery<T>(
  gateway: HermesGateway,
  method: string,
  params: Record<string, unknown> | undefined,
  timeoutMs: number | undefined,
  signal: AbortSignal | undefined,
  strict: boolean
): Promise<T> {
  const ready = await waitForPrimaryGateway(gateway, timeoutMs, signal)

  if (strict && (!ready || gateway.connectionState !== 'open')) {
    throw new Error('Hermes gateway unavailable')
  }

  const remainingMs = ready ? ready.remainingMs : timeoutMs

  return remainingMs === undefined && signal === undefined
    ? gateway.request<T>(method, params)
    : gateway.request<T>(method, params, remainingMs, signal)
}
