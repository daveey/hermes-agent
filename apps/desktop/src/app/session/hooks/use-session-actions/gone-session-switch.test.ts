import { afterEach, describe, expect, it } from 'vitest'

import { $gatewaySwitching } from '@/store/gateway-switch'
import { $activeGatewayProfile, $gatewaySwapTarget } from '@/store/profile'

import { goneSessionSwitchInFlight } from './gone-session-switch'

describe('goneSessionSwitchInFlight', () => {
  const initial = {
    profile: $activeGatewayProfile.get(),
    swap: $gatewaySwapTarget.get(),
    switching: $gatewaySwitching.get()
  }

  afterEach(() => {
    $activeGatewayProfile.set(initial.profile)
    $gatewaySwapTarget.set(initial.swap)
    $gatewaySwitching.set(initial.switching)
  })

  it('is false for an unknown or matching owner with no switch in flight', () => {
    $gatewaySwitching.set(false)
    $gatewaySwapTarget.set(null)
    $activeGatewayProfile.set('default')
    expect(goneSessionSwitchInFlight(undefined)).toBe(false)
    expect(goneSessionSwitchInFlight('  ')).toBe(false)
    expect(goneSessionSwitchInFlight('default')).toBe(false)
  })

  it('is true for a known foreign owner or a switch in flight', () => {
    $gatewaySwitching.set(false)
    $gatewaySwapTarget.set(null)
    $activeGatewayProfile.set('default')
    expect(goneSessionSwitchInFlight('work')).toBe(true)
    $gatewaySwitching.set(true)
    expect(goneSessionSwitchInFlight('default')).toBe(true)
  })
})
