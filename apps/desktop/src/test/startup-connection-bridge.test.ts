// @vitest-environment node
import { expect, it } from 'vitest'

import {
  backendReadinessTimeout,
  isBackendReadinessTimeout,
  isStartupDeadlineResult,
  unwrapStartupConnection
} from '../../../shared/src/backend-readiness-timeout'

import { startupConnectionBridge } from './startup-connection-bridge'

it.each([null, { profile: 'worker' }, { connectionId: 'local', profile: 'worker' }])(
  'copies structured deadlines through both IPC and contextBridge: %j',
  async route => {
    const api = startupConnectionBridge(async () => {
      throw backendReadinessTimeout('wording intentionally unrelated')
    })
    const result = await api.getStartupConnection(route)
    expect(result).toEqual({ ok: false, reason: 'startup_deadline', message: 'wording intentionally unrelated' })
    let error: unknown

    try {
      unwrapStartupConnection(result)
    } catch (caught) {
      error = caught
    }
    expect(isBackendReadinessTimeout(error)).toBe(true)
    expect(isBackendReadinessTimeout(error, 'connected')).toBe(false)
    await expect(api.getConnection()).rejects.toThrow('wording intentionally unrelated')
    await expect(api.getConnectionFor({ connectionId: 'local' })).rejects.toThrow('wording intentionally unrelated')
  }
)

it('preserves successful legacy descriptors and structured startup descriptors', async () => {
  const connection = { baseUrl: 'http://127.0.0.1:9119', profile: 'worker' }
  const api = startupConnectionBridge(async () => connection)
  expect(await api.getConnection()).toEqual(connection)
  expect(await api.getConnectionFor({ connectionId: 'local' })).toEqual(connection)
  expect(await api.getStartupConnection()).toEqual({ ok: true, connection })
})

it.each(['EACCES', 'Sign in again', 'Gateway starting (deadline).', 'Timed out connecting to Hermes backend'])(
  'never promotes unmarked errors based on diagnostic text: %s',
  async message => {
    const api = startupConnectionBridge(async () => {
      throw new Error(message)
    })
    const error = await api.getStartupConnection().catch((error: unknown) => error)
    expect((error as Error).message).toContain("Error invoking remote method 'hermes:connection'")
    expect(isBackendReadinessTimeout(error)).toBe(false)
  }
)

it.each([
  null,
  {},
  { ok: true, reason: 'startup_deadline', message: 'x' },
  { ok: false, reason: 'unrelated', message: 'x' },
  { ok: false, reason: 'startup_deadline' }
])('rejects incomplete/mismatched deadline envelopes: %j', value => {
  expect(isStartupDeadlineResult(value)).toBe(false)

  if (value && 'ok' in value && value.ok === false) {
    expect(() => unwrapStartupConnection(value as never)).toThrow('Invalid startup connection result')
  }
})
