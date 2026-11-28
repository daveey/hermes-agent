import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { expect, test, vi } from 'vitest'

import { destroyKeepaliveAgents } from './api-transport'
import { fetchJsonWith } from './json-fetch'
import { capturePrimarySshTeardown, createPrimaryRemoteRevalidator } from './primary-remote-revalidation'
import { RemoteLivenessTracker, RemoteRevalidationCoordinator } from './remote-liveness'
import * as tracing from './request-stages'

function capture() {
  const rows: any[] = []
  const buffer = new tracing.StageBuffer(async lines => {
    rows.push(
      ...lines
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
    )
  })
  const observe = (event: Parameters<typeof tracing.observePrimaryRevalidation>[0], fields = {}) =>
    tracing.observePrimaryRevalidation(event, fields, buffer)

  return { rows, buffer, observe }
}

test('real probe wire ID joins round/generation, not the unrelated business request ID', async () => {
  const { rows, buffer, observe } = capture()
  const actual = tracing.requestStages
  const spy = vi.spyOn(tracing, 'requestStages').mockImplementation((url, method) => actual(url, method, buffer))
  let wireId = ''

  const server = http.createServer((req, res) => {
    wireId = String(req.headers['x-hermes-req-id'])
    res.end('{}')
  })

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
  const cp = Promise.resolve({ mode: 'remote', baseUrl })

  const revalidate = createPrimaryRemoteRevalidator({
    currentConnectionPromise: () => cp,
    coordinator: new RemoteRevalidationCoordinator(),
    tracker: new RemoteLivenessTracker(),
    observe,
    log() {},
    resetConnection() {},
    captureSshTeardown: () => async () => {},
    probe: (connection, path, options) =>
      fetchJsonWith(
        { remoteHeaders: () => ({}), nativeHeaders: async () => null },
        connection.baseUrl! + path + '?secret=PRIVATE',
        null,
        { ...options, pool: 'probe' }
      )
  })

  try {
    await tracing.withRegistryRevalidationStages(
      { forceProbe: true, revalidationTrigger: 'prewarm' },
      () => revalidate('prewarm'),
      buffer
    )
    await buffer.flush()
    const wire = rows.find(r => r.stage === 'call')
    const invocation = rows.find(r => r.event === 'invocation')
    expect(wire.id).toBe(wireId)
    expect(wire).toMatchObject({
      path: '/api/status',
      round: invocation.round,
      generation: invocation.generation,
      invocation: invocation.invocation
    })
    expect(wire.round).toBeGreaterThan(0)
    expect(rows.filter(r => r.id === wireId).some(r => r.stage === 'response_end')).toBe(true)
    expect(rows.filter(r => r.event === 'probe_success')).toHaveLength(1)
    expect(
      rows.every(
        r =>
          typeof r.boot === 'string' &&
          r.pid > 0 &&
          r.seq > 0 &&
          Number.isFinite(r.mono_ms) &&
          Number.isFinite(r.utc_ms) &&
          r.dropped === 0 &&
          r.io_lost === 0
      )
    ).toBe(true)
    expect(JSON.stringify(rows)).not.toContain('PRIVATE')
  } finally {
    spy.mockRestore()
    destroyKeepaliveAgents()
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
})

test.each(['missing_scope', 'stale_scope', 'stale_scope_after_drain', 'teardown_error'] as const)(
  'explicit captured teardown outcome: %s',
  async outcome => {
    const { rows, buffer, observe } = capture()
    const old = { localPort: 32123 }
    const fresh = { localPort: 32124 }
    const states = new Map(outcome === 'missing_scope' ? [] : [['PRIVATE_SCOPE', old]])

    const teardown = capturePrimarySshTeardown(
      { baseUrl: 'http://127.0.0.1:32123' },
      {
        states,
        observe,
        cancelAndWait: async (_key, run) => {
          if (outcome === 'stale_scope_after_drain') {
            states.set('PRIVATE_SCOPE', fresh)
          }
          await run()
        },
        teardown: async () => {
          throw Error('PRIVATE_ERROR')
        }
      }
    )

    if (outcome === 'stale_scope') {
      states.set('PRIVATE_SCOPE', fresh)
    }

    if (outcome === 'teardown_error') {
      await expect(teardown()).rejects.toThrow('PRIVATE_ERROR')
    } else {
      await teardown()
    }
    await buffer.flush()
    expect(rows.some(r => r.event === outcome)).toBe(true)
    expect(JSON.stringify(rows)).not.toContain('PRIVATE')
    expect(rows.some(r => r.event === 'teardown_end')).toBe(false)
  }
)

test('registry rejection has a terminal identity without exporting the exception', async () => {
  const { rows, buffer } = capture()
  const error = Error('PRIVATE_CREDENTIAL')
  await expect(
    tracing.withRegistryRevalidationStages(
      { forceProbe: true },
      async () => {
        throw error
      },
      buffer
    )
  ).rejects.toBe(error)
  await buffer.flush()
  expect(rows.map(r => r.event)).toEqual(['registry_start', 'registry_error'])
  expect(rows[0].invocation).toBe(rows[1].invocation)
  expect(JSON.stringify(rows)).not.toContain('PRIVATE')
})

test.each(['rejected', 'local', 'stale_before', 'stale_after_success', 'stale_after_failure'] as const)(
  'observes non-retiring verdict %s',
  async kind => {
    const { rows, buffer, observe } = capture()
    const cp =
      kind === 'rejected'
        ? Promise.reject(Error('PRIVATE'))
        : Promise.resolve({ mode: kind === 'local' ? 'local' : 'remote', baseUrl: 'http://127.0.0.1:12345' })
    let current: typeof cp | null = kind === 'stale_before' ? null : cp
    let reset = false

    const revalidate = createPrimaryRemoteRevalidator({
      observe,
      currentConnectionPromise: () => current || cp,
      coordinator: new RemoteRevalidationCoordinator(),
      tracker: new RemoteLivenessTracker(),
      log() {},
      resetConnection: () => {
        reset = true
      },
      captureSshTeardown: () => async () => {},
      probe: async () => {
        current = Promise.resolve({ mode: 'remote', baseUrl: 'http://127.0.0.1:23456' })

        if (kind === 'stale_after_failure') {
          throw Error('PRIVATE')
        }
      }
    })

    // A separate helper tests pre-probe stale identity, while the factory's current promise is fixed at admission.
    if (kind === 'stale_before') {
      const { revalidateRemoteConnection } = await import('./remote-liveness')
      await revalidateRemoteConnection({
        connectionPromise: cp,
        currentConnectionPromise: () => null,
        observe,
        tracker: new RemoteLivenessTracker(),
        log() {},
        probe: async () => {
          throw Error('must not probe')
        },
        resetConnection() {}
      })
    } else {
      await revalidate()
    }

    await buffer.flush()
    const verdict = kind === 'rejected' ? 'connection_rejected' : kind === 'local' ? 'not_remote' : 'stale_connection'
    expect(rows.some(r => r.event === verdict)).toBe(true)
    expect(reset).toBe(false)
    expect(rows.some(r => r.event === 'probe_failure')).toBe(false)
    expect(JSON.stringify(rows)).not.toContain('PRIVATE')
  }
)
