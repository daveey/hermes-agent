import { describe, expect, it } from 'vitest'

import { isSessionBusyRefusal } from './session-busy-refusal'

describe('isSessionBusyRefusal', () => {
  it('treats a busy chat as a refused send', () => {
    expect(isSessionBusyRefusal({ failureReason: 'session_busy' })).toBe(true)
    expect(isSessionBusyRefusal({ error: 'session_turn_lease_timeout:abc' })).toBe(true)
  })

  // A route behind a subscription quota wall refuses the turn before the agent is built: the message never
  // ran, so the composer offers it back (hub chat 20261111_213321_be8afc, 11-21).
  it('treats a quota-walled route refusal as a refused send', () => {
    expect(isSessionBusyRefusal({ surfaceCode: 'route_refused' })).toBe(true)
  })

  it('leaves other failures alone', () => {
    expect(isSessionBusyRefusal({ surfaceCode: 'agent_init_failed' })).toBe(false)
    expect(isSessionBusyRefusal({})).toBe(false)
  })
})
