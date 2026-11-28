/**
 * `ui.new_session` seams for `use-session-actions`, kept out of that file (at its line cap) and testable
 * on their own. The finisher is the composer probe in `@/lib/ui-latency-probes`.
 */

import {
  bindUiLatency,
  cancelUiLatency,
  startUiLatency,
  UI_LATENCY_UNBOUND,
  type UiLatencyToken
} from '@/lib/ui-latency'

import { type FreshSessionDraftOptions, isUserFreshDraft } from './fresh-draft-options'

export const newSessionLatency = {
  /** A fresh draft: the clock starts only for a user's New session (see `isUserFreshDraft`); the span ends on an
   *  unbound editable composer. */
  draft(options: boolean | FreshSessionDraftOptions): UiLatencyToken | undefined {
    return isUserFreshDraft(options) ? startUiLatency('ui.new_session', {}, { runtimeId: null }) : undefined
  },
  /** A tile "+": the new session's own composer ends it, so nothing matches until `tileCreated` names it. */
  tileClick(): UiLatencyToken {
    return startUiLatency('ui.new_session', {}, { runtimeId: UI_LATENCY_UNBOUND })
  },
  tileCreated(
    token: UiLatencyToken,
    route: null | undefined | { connectionId?: string; profile?: string },
    stored: string,
    runtimeId: string
  ) {
    bindUiLatency(
      'ui.new_session',
      { gateway: route?.connectionId, profile: route?.profile, session_id: stored },
      { runtimeId },
      token
    )
  },
  /** The create failed or answered without a session: no composer will ever show. */
  cancel(token: UiLatencyToken | undefined): void {
    cancelUiLatency('ui.new_session', token)
  }
}
