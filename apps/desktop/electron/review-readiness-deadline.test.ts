import { expect, it } from 'vitest'

import { isBackendReadinessTimeout } from '../../shared/src/backend-readiness-timeout'

import { shouldLatchBackendStartFailure } from './backend-start-failure'
import { ensureLocalGateway } from './local-gateway'

it.each([undefined, 'Still starting.'])('canonical starting/deadline remains recoverable (detail: %s)', async detail => {
  const error = await ensureLocalGateway(async () => ({
    code: 5,
    stdout: JSON.stringify({ state: 'starting', reason_code: 'deadline', detail })
  })).catch((error: unknown) => error)

  expect(error).toBeInstanceOf(Error)
  expect(isBackendReadinessTimeout(error)).toBe(true)
  expect(shouldLatchBackendStartFailure({ attemptedRemote: false, error })).toBe(false)
  expect(isBackendReadinessTimeout(error, 'connecting')).toBe(false)
})

it.each([
  { state: 'incompatible', reason_code: 'deadline' },
  { state: 'starting', reason_code: 'update_paused' },
  { state: 'failed', reason_code: 'deadline' }
])('does not retry a terminal ensure verdict: %j', async verdict => {
  const error = await ensureLocalGateway(async () => ({ code: 5, stdout: JSON.stringify(verdict) })).catch((error: unknown) => error)
  expect(isBackendReadinessTimeout(error)).toBe(false)
  expect(shouldLatchBackendStartFailure({ attemptedRemote: false, error })).toBe(true)
})
