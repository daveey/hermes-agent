import { expect, test, vi } from 'vitest'

import { createRegistryRetirementClient } from './registry-retirement-http'

const descriptor = { baseUrl: 'http://127.0.0.1:31000', token: 'synthetic-token' }

test('ownership remains provable for a deleted runtime without treating it as reusable', async () => {
  const request = vi.fn(async () => ({ ok: true, protocolVersion: 1, sshOwnerNonce: 'nonce', runtimeIntact: false }))
  const client = createRegistryRetirementClient(request)
  expect(await client.proveOwned(descriptor, 'nonce')).toBe(true)
  expect(await client.proveOwned(descriptor, 'other')).toBe(false)
  expect(request).toHaveBeenCalledWith(descriptor, '/api/ssh/ownership', { timeoutMs: 3000 })
})

test.each([null, {}, { ok: true, idle: null }, { ok: true, idle: false, token: 'permit' }, { ok: true, idle: true }])(
  'unknown or busy preparation grants no permit: %j',
  async reply => {
    const client = createRegistryRetirementClient(vi.fn(async () => reply))
    expect(await client.prepare(descriptor)).toBeNull()
  }
)

test('uses the exact authenticated descriptor for prepare, commit and cancellation', async () => {
  const request = vi.fn(async () => ({ ok: true, idle: true, token: 'permit' }))
  const client = createRegistryRetirementClient(request)
  expect(await client.prepare(descriptor)).toBe('permit')
  expect(await client.commit(descriptor, 'permit')).toBe(true)
  await client.cancel(descriptor, 'permit')
  expect(request.mock.calls).toEqual(
    ['prepare', 'commit', 'cancel'].map(action => [
      descriptor,
      '/api/health/retirement',
      {
        method: 'POST',
        timeoutMs: 3000,
        body: { action, ...(action === 'prepare' ? {} : { token: 'permit' }) }
      }
    ])
  )
})

test.each([null, {}, { ok: 'true' }])(
  'ambiguous commit response throws rather than releasing the fence: %j',
  async reply => {
    const client = createRegistryRetirementClient(vi.fn(async () => reply))
    await expect(client.commit(descriptor, 'permit')).rejects.toThrow('Unknown retirement commit outcome')
  }
)

test('transport loss at commit propagates while a definite refusal returns false', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('lost response')).mockResolvedValueOnce({ ok: false })
  const client = createRegistryRetirementClient(request)
  await expect(client.commit(descriptor, 'permit')).rejects.toThrow('lost response')
  expect(await client.commit(descriptor, 'permit')).toBe(false)
})
