import { $gatewaySwitching } from '@/store/gateway-switch'
import { $activeGatewayProfile, $gatewaySwapTarget, normalizeProfileKey } from '@/store/profile'

/**
 * `switchInFlight` for {@link goneSessionVerdict}: a 404 on resume is only
 * trustworthy from the backend that owns the session. A profile swap or
 * connection switch in flight (or just targeted) means "retry once things
 * settle". So does a KNOWN owner that is not the active gateway: the 404 came
 * from the wrong backend. An UNKNOWN owner must not count — it would block the
 * draft fallback for genuinely dead ids on secondary profiles.
 */
export function goneSessionSwitchInFlight(sessionProfile: null | string | undefined): boolean {
  return (
    $gatewaySwitching.get() ||
    Boolean($gatewaySwapTarget.get()) ||
    Boolean(
      sessionProfile?.trim() && normalizeProfileKey(sessionProfile) !== normalizeProfileKey($activeGatewayProfile.get())
    )
  )
}
