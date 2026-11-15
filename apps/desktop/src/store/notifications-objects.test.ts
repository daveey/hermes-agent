import { afterEach, expect, test, vi } from 'vitest'

import { $notifications, clearNotifications, notifyError, readableError } from './notifications'

const fallback = 'Prompt failed'
afterEach(() => {
  clearNotifications()
  vi.unstubAllGlobals()
})

test.each([
  { message: 'Reconnect to the gateway' },
  { detail: { message: 'Reconnect to the gateway' } },
  { error: { code: -32000, data: { detail: 'Reconnect to the gateway' } } },
  { message: 'RPC failed', data: { message: 'Reconnect to the gateway' } }
])('preserves a structured message in the toast and log: %j', error => {
  const logLine = vi.fn()
  vi.stubGlobal('hermesDesktop', { logLine })
  notifyError(error, fallback)
  expect($notifications.get()[0]).toMatchObject({ title: fallback, message: 'Reconnect to the gateway' })
  expect(logLine.mock.calls[0][0]).toContain('Reconnect to the gateway')
})

test('extracts only diagnostic fields without serializing secrets or invoking getters', () => {
  const getter = vi.fn(() => {
    throw new Error('must not run')
  })

  const error = {
    detail: { message: 'Reconnect' },
    token: 'secret-token',
    request: { password: 'secret-password' },
    toJSON: getter
  }

  Object.defineProperty(error, 'message', { get: getter })
  const logLine = vi.fn()
  vi.stubGlobal('hermesDesktop', { logLine })
  notifyError(error, fallback)
  expect($notifications.get()[0]?.message).toBe('Reconnect')
  expect(logLine.mock.calls[0][0]).not.toMatch(/secret|password|token/)
  expect(getter).not.toHaveBeenCalled()
})

test('handles circular, deep and empty objects with bounded detail and no duplicate heading', () => {
  const circular: { detail?: unknown; message?: string } = {}
  circular.detail = circular
  expect(readableError(circular, fallback).message).toBe(fallback)
  circular.message = 'Reconnect'
  expect(readableError(circular, fallback).message).toBe('Reconnect')
  let deep: unknown = { message: 'too deep' }

  for (let i = 0; i < 30; i++) {
    deep = { detail: deep }
  }

  expect(readableError(deep, fallback).message).toBe(fallback)
  notifyError({}, fallback)
  expect($notifications.get()[0]?.title).toBeUndefined()
  notifyError({ message: 'x'.repeat(100_000) }, fallback)
  expect($notifications.get()[0]?.title).toBeUndefined()
  expect($notifications.get()[0]?.detail?.length).toBeLessThanOrEqual(8192)
  expect($notifications.get()[0]?.detail).toContain('xxx')
})

test.each([
  { code: 'disk_full' },
  { code: 'disk_full', message: 'No space left on device' },
  { error: { message: 'Request refused', data: { code: 'disk_full' } } }
])('preserves specific disk-full recovery and telemetry: %j', error => {
  const result = readableError(error, fallback)
  expect(result.category).toBe('disk_full')
  expect(result.message).toBe('Disk full — free some space, then try again.')
  expect(result.action).toBeDefined()
  notifyError(error, fallback)
  expect($notifications.get()[0]?.message).toBe(result.message)
})

test.each(['storage_locked', 'disk_full', 'gateway_auth_failed', 'invalid_api_key'])(
  'retains classification code %s',
  code => {
    const result = readableError({ error: { message: 'Request refused', data: { code } } }, fallback)
    expect(result.action).toBeDefined()
    expect(result.category).not.toBe('unclassified')
  }
)
