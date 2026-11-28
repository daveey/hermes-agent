import { describe, expect, it } from 'vitest'

import { formatApiRequestFailure } from './api-request-failure'
import { RemoteBackendDownError } from './remote-down-gate'

const timeout = new Error('Timed out connecting to Hermes backend after 4000ms')

describe('formatApiRequestFailure', () => {
  it('names the registry connection a failed request was pinned to', () => {
    // hub-sync asks EVERY connection for /status; a remote tunnel timing out must not read as the local backend.
    const line = formatApiRequestFailure(
      { connectionId: 'conn:mac-mini', method: 'get', path: '/api/plugins/hub-sync/status' } as any,
      timeout
    )

    expect(line.startsWith('[hermes:api GET /api/plugins/hub-sync/status] conn=conn:mac-mini ')).toBe(true)
    expect(line).toContain('Timed out connecting to Hermes backend after 4000ms')
  })

  it('marks an unpinned request as the primary route, with its profile when given', () => {
    expect(formatApiRequestFailure({ path: '/api/plugins/kanban/boards' }, timeout)).toMatch(
      /^\[hermes:api GET \/api\/plugins\/kanban\/boards\] conn=primary Error: Timed out/
    )
    expect(formatApiRequestFailure({ path: '/api/sessions', profile: 'coach' } as any, timeout)).toMatch(
      /^\[hermes:api GET \/api\/sessions\] conn=primary profile=coach Error:/
    )
  })

  it('keeps the coach backend-timeouts parser working (path still closes the bracket)', () => {
    const line = formatApiRequestFailure({ connectionId: 'conn:hermes-hub', path: '/api/x?y=1' } as any, timeout)
    expect(/\[hermes:api (?:[A-Z]+ )?(\S+?)\]/.exec(line)?.[1]).toBe('/api/x?y=1')
  })

  it('clamps long paths, ids and details', () => {
    const line = formatApiRequestFailure(
      { connectionId: 'c'.repeat(500), path: '/p'.repeat(400), profile: 'q'.repeat(500) } as any,
      new Error('e'.repeat(9000))
    )

    expect(line.length).toBeLessThanOrEqual(6000)
    expect(line).toContain(`conn=${'c'.repeat(120)} `)
  })
  it('logs a remote-down fast-fail as one line without a stack', () => {
    const fastFail = new RemoteBackendDownError('conn:hermes-hub::default', 'read ECONNRESET', 3000, 1000)
    const line = formatApiRequestFailure(
      { connectionId: 'hermes-hub', path: '/api/plugins/hub-sync/status' } as any,
      fastFail
    )

    expect(line).toContain('conn=hermes-hub Remote backend "conn:hermes-hub::default" is down')
    expect(line).not.toContain('\n')
    expect(line).not.toContain('Timed out connecting')
  })
})
