import { expect, test } from 'vitest'

import {
  capturePrimarySshTeardown,
  createPrimaryRemoteRevalidator,
  ensureProbedPrimaryBackend
} from './primary-remote-revalidation'
import { RemoteLivenessTracker, RemoteRevalidationCoordinator } from './remote-liveness'
import { requestStages, StageBuffer } from './request-stages'

function observer() {
  const events: { event: string; [key: string]: unknown }[] = []
  const observe = (event: string, fields = {}) => events.push({ ...fields, event })

  return { events, observe }
}

test('the real health probe path is admitted without query credentials', async () => {
  const rows: any[] = []
  const buffer = new StageBuffer(async lines => {
    rows.push(
      ...lines
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
    )
  })
  const trace = requestStages('http://127.0.0.1:32123/api/status?secret=PRIVATE', 'GET', buffer)
  expect(trace).toBeDefined()
  await buffer.flush()
  expect(rows[0]).toMatchObject({ path: '/api/status', port: 32123, stage: 'call' })
  expect(JSON.stringify(rows)).not.toContain('PRIVATE')
})

test('coalesced callers have one round and terminal probe verdict, not caller-count strikes', async () => {
  const { events, observe } = observer()
  const cp = Promise.resolve({ mode: 'remote', remoteKind: 'ssh', baseUrl: 'http://127.0.0.1:32123?PRIVATE' })
  let release!: () => void
  const blocked = new Promise<void>(resolve => {
    release = resolve
  })
  let probes = 0

  const revalidate = createPrimaryRemoteRevalidator({
    currentConnectionPromise: () => cp,
    coordinator: new RemoteRevalidationCoordinator(),
    tracker: new RemoteLivenessTracker(),
    log() {},
    observe,
    probe: async () => {
      probes++
      await blocked
    },
    resetConnection() {},
    captureSshTeardown: () => async () => {}
  })

  const pending = Array.from({ length: 8 }, () => revalidate('prewarm'))
  release()
  await Promise.all(pending)
  expect(probes).toBe(1)
  expect(events.filter(e => e.event === 'invocation')).toHaveLength(8)
  expect(events.filter(e => e.event === 'invocation' && e.joined)).toHaveLength(7)
  expect(new Set(events.filter(e => e.event === 'invocation').map(e => e.round))).toHaveLength(1)
  expect(events.filter(e => e.event === 'probe_start')).toHaveLength(1)
  expect(events.find(e => e.event === 'probe_success')).toMatchObject({ failures: 0 })
  expect(events.filter(e => e.event === 'round_end')).toHaveLength(1)
  expect(JSON.stringify(events)).not.toContain('PRIVATE')
})

test('retirement records captured identity, drain before reset, and awaited teardown', async () => {
  const { events, observe } = observer()
  const state = { localPort: 32123 }
  const states = new Map([['PRIVATE_SCOPE', state]])
  const cp = Promise.resolve({ mode: 'remote', remoteKind: 'ssh', baseUrl: 'http://127.0.0.1:32123' })
  let current: typeof cp | null = cp

  const revalidate = createPrimaryRemoteRevalidator({
    currentConnectionPromise: () => current,
    coordinator: new RemoteRevalidationCoordinator(),
    tracker: new RemoteLivenessTracker(1),
    log() {},
    observe,
    probe: async () => {
      throw Error('PRIVATE_ERROR')
    },
    resetConnection: () => {
      current = null
    },
    captureSshTeardown: connection =>
      capturePrimarySshTeardown(connection, {
        states,
        observe,
        cancelAndWait: async (_key, stop) => {
          await stop()
        },
        teardown: async () => {
          states.delete('PRIVATE_SCOPE')
        }
      })
  })

  await expect(revalidate('renderer')).resolves.toEqual({ ok: true, rebuilt: true })
  const names = events.map(e => e.event)
  expect(names).toContain('probe_failure')
  expect(events.find(e => e.event === 'probe_failure')).toMatchObject({ failures: 1, should_reset: true })

  for (const e of ['retirement_requested', 'drain_start', 'reset', 'teardown_end', 'round_end']) {
    expect(names).toContain(e)
  }
  expect(names.indexOf('drain_start')).toBeLessThan(names.indexOf('reset'))
  expect(names.indexOf('teardown_end')).toBeLessThan(names.indexOf('round_end'))
  expect(events.find(e => e.event === 'ssh_capture')).toMatchObject({ port: 32123, matched: true })
  expect(JSON.stringify(events)).not.toContain('PRIVATE')
})

test('ordinary/passive matches record skip and never probe', async () => {
  const { events, observe } = observer()
  let probes = 0

  for (const opts of [{}, { forceProbe: true, passive: true }]) {
    expect(
      await ensureProbedPrimaryBackend(42, opts, {
        observe,
        revalidate: async () => {
          probes++
        },
        ensure: async () => 43
      })
    ).toBe(42)
  }

  expect(probes).toBe(0)
  expect(events.map(e => e.event)).toEqual(['matched_primary', 'matched_primary'])
  expect(events.every(e => e.skipped === true)).toBe(true)
})
