import assert from 'node:assert/strict'

import { test } from 'vitest'

import { normalizeRegistry, resolveRegistryLocalRoute, reuseMatchingPrimaryRemoteBackend } from './connection-registry'
import { resolveDesktopRemoteRoute } from './desktop-remote-route'
import { EnvironmentRegistry } from './environment-registry'

const env = { url: 'https://env.test', token: 'process-only-token' }

test('env primary has an owner and registry requests reuse it without a local spawn', async () => {
  const stored = normalizeRegistry(null)
  const overlay = new EnvironmentRegistry(env)
  const registry = overlay.read(stored)
  assert.equal(overlay.read(stored), registry)
  const route = resolveDesktopRemoteRoute({ config: { mode: 'local' }, env, registry })
  assert.equal(route?.connectionId, registry.primary)

  const reused = await reuseMatchingPrimaryRemoteBackend({
    registry,
    connectionId: registry.primary,
    source: registry.connections.find(c => c.id === registry.primary)!,
    profile: 'research',
    ensurePrimary: async () => ({ mode: 'remote', connectionId: route?.connectionId })
  })

  assert.equal(reused?.sharedRemote, true)
  assert.equal(reused?.profile, 'research')
  assert.equal(resolveRegistryLocalRoute('default', { globalRemote: true }).delegate, false)
})

test('env selection and credentials never persist, but saved source edits survive', () => {
  const stored = normalizeRegistry({
    primary: 'saved',
    lastUsed: 'saved',
    connections: [
      { id: 'saved', kind: 'remote', label: 'Saved', url: 'https://saved.test' },
      { id: 'environment-gateway', kind: 'remote', label: 'Existing', url: 'https://existing.test' }
    ]
  })

  const overlay = new EnvironmentRegistry(env)
  const registry = overlay.read(stored)
  assert.notEqual(registry.primary, 'environment-gateway')

  const next = {
    ...registry,
    lastUsed: registry.primary,
    connections: registry.connections.map(c => (c.id === 'saved' ? { ...c, label: 'Renamed' } : c))
  }

  const persisted = overlay.stored(next, stored)
  assert.equal(persisted.primary, 'saved')
  assert.equal(persisted.lastUsed, 'saved')
  assert.equal(persisted.connections.find(c => c.id === 'saved')?.label, 'Renamed')
  assert.equal(JSON.stringify(persisted).includes(env.token), false)
  assert.equal(JSON.stringify(persisted).includes(env.url), false)
  assert.throws(() => overlay.assertEditable(registry.primary), /environment/)
  overlay.assertEditable('local')
  assert.equal(new EnvironmentRegistry({}).read(persisted), persisted)
})
