import { expect, test, vi } from 'vitest'

import { ManagedConnectionUpdateGate } from './managed-ssh-update'
import { createSshReuseProbe, wireRegistryReconnect } from './registry-reconnect-wiring'

test('wiring gates only new SSH dials with the exact correlation', () => {
  const gate = new ManagedConnectionUpdateGate()
  const handle = vi.fn()

  const service = wireRegistryReconnect({
    ipcMain: { handle },
    pool: new Map(),
    states: new Map(),
    gate,
    key: (id, profile) => `${id}:${profile}`,
    readRegistry: () => ({ connections: [] }),
    dialClaims: { inFlight: () => false },
    poolStopper: { inFlight: () => false },
    bootstrap: { pending: new Set<string>() },
    request: async () => null,
    keepalives: { stop: vi.fn() },
    disposeTerminals: vi.fn(),
    log: vi.fn(),
    reopen: async () => {}
  })

  const owner = '11111111-1111-4111-8111-111111111111'
  gate.claim('hub', owner)
  expect(() => service.assertCanDial('ssh', 'hub', '')).toThrow()
  expect(() => service.assertCanDial('ssh', 'hub', 'other')).toThrow()
  expect(() => service.assertCanDial('ssh', 'hub', owner)).not.toThrow()
  expect(() => service.assertCanDial('ssh', 'other', '')).not.toThrow()
  expect(() => service.assertCanDial('remote', 'hub', '')).not.toThrow()
  expect(() => service.assertCanDial('local', 'hub', '')).not.toThrow()
  expect(handle.mock.calls.map(call => call[0])).toEqual([
    'hermes:connections:reconnect-target',
    'hermes:connections:reconnect'
  ])
  expect(handle.mock.calls[0][1](null, 'hub', 'default')).toBeNull()
})

test.each([401, 403, 404])('reuse probe classifies authenticated HTTP %s as stale', async status => {
  const fetchJson = vi.fn(async () => {
    throw new Error(`${status}: unavailable`)
  })
  await expect(createSshReuseProbe(fetchJson)('http://hub', 'token', 'nonce')).resolves.toBe('authenticated-stale')
  expect(fetchJson).toHaveBeenCalledWith('http://hub/api/ssh/ownership', 'token')
})

test('reuse probe preserves transport errors and ownership classification', async () => {
  const error = new Error('connection reset')
  await expect(
    createSshReuseProbe(async () => {
      throw error
    })('http://hub', 'token', 'nonce')
  ).rejects.toBe(error)
  const probe = createSshReuseProbe(async () => ({ ok: true, sshOwnerNonce: 'nonce', protocolVersion: 1 }))
  await expect(probe('http://hub', 'token', 'nonce')).resolves.toBe('authenticated-ok')
})
