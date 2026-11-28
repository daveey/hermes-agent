import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import { $gateway, setPrimaryGatewayConnectionId } from './gateway'
import { setHapticsMuted } from './haptics'
import {
  clearPluginNotifyHandlers,
  dispatchNativeNotification,
  dispatchPluginNativeNotification,
  invokePluginNotifyAction,
  invokePluginNotifyActivate,
  NATIVE_NOTIFICATION_KINDS,
  respondToApprovalAction,
  sendTestNativeNotification,
  setNativeNotifyEnabled,
  setNativeNotifyKind
} from './native-notifications'
import { __resetNativeNotifyBaselineForTests, markNativeNotifyBaseline } from './notify-baseline'
import { $approvalRequest, APPROVAL_RESPOND_REQUEST_TIMEOUT_MS, clearAllPrompts, setApprovalRequest } from './prompts'
import { markSessionGone, resetBackgroundPollingGuard } from './runtime-gone'
import { setActiveSessionId, setSessions } from './session'
import { stampSecondaryProfileOwner } from './session-event-provenance'
import { dropSessionState, publishSessionState, recordSessionEventScope } from './session-states'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const notify = vi.fn().mockResolvedValue(true)

function setWindowState({ focused = true, hidden = false }: { focused?: boolean; hidden?: boolean }) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden })
  Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => focused })
}

let counter = 0

// Unique session id per call dodges the per-(kind,session) throttle so each
// assertion starts clean.
function freshSession(): string {
  counter += 1

  return `session-${counter}`
}

beforeEach(() => {
  notify.mockClear()
  desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
  setNativeNotifyEnabled(true)

  for (const kind of NATIVE_NOTIFICATION_KINDS) {
    setNativeNotifyKind(kind, true)
  }

  setActiveSessionId(null)
  resetBackgroundPollingGuard()
  setWindowState({ focused: false, hidden: true })
  __resetNativeNotifyBaselineForTests()
})

afterEach(() => {
  clearPluginNotifyHandlers()

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }

  resetBackgroundPollingGuard()
})

it('captures durable navigation identity while keeping the runtime id for approval actions', () => {
  const runtimeId = freshSession()
  publishSessionState(runtimeId, createClientSessionState('durable-chat'))

  try {
    dispatchNativeNotification({ kind: 'approval', sessionId: runtimeId, title: 'Approval' })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: runtimeId, focusSessionId: 'durable-chat' })
    )
  } finally {
    dropSessionState(runtimeId)
  }
})

describe('dispatchNativeNotification focus gating', () => {
  it.each(['missing', 'unstamped', 'stale', 'future', 'ambiguous'])('fails closed on %s projection', state => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    const observed = Date.now() / 1000
    const row = { id: sessionId, kanban_observed_at: observed }
    setSessions(
      (state === 'missing'
        ? []
        : state === 'unstamped'
          ? [{ id: sessionId }]
          : state === 'ambiguous'
            ? [row, { ...row, profile: 'other' }]
            : [{ ...row, kanban_observed_at: observed + (state === 'future' ? 60 : -181) }]) as never
    )
    expect(dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })).toBe(false)
    expect(notify).not.toHaveBeenCalled()
  })

  it('delivers once when a post-turn projection verifies release, not on an older zero', () => {
    vi.useFakeTimers()
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    publishSessionState(runtime, createClientSessionState(stored))
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: endedAt - 1 } as never])
      expect(
        dispatchNativeNotification({
          kind: 'turnDone',
          sessionId: runtime,
          title: 'done',
          projectionAfter: endedAt
        } as never)
      ).toBe(false)
      expect(notify).not.toHaveBeenCalled()
      vi.advanceTimersByTime(5000)
      setSessions([
        { id: stored, profile: 'default', kanban_observed_at: Date.now() / 1000, kanban_active: 1 } as never
      ])
      expect(notify).not.toHaveBeenCalled()
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: Date.now() / 1000 } as never])
      expect(notify).toHaveBeenCalledTimes(1)
      vi.advanceTimersByTime(5000)
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: Date.now() / 1000 } as never])
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
      vi.useRealTimers()
    }
  })

  it('refreshes the projection while backgrounded until verified release', async () => {
    vi.useFakeTimers()
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    publishSessionState(runtime, createClientSessionState(stored))
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    const refreshProjection = vi.fn(async () => {
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: Date.now() / 1000 } as never])
    })

    try {
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: endedAt - 1 } as never])
      dispatchNativeNotification({
        kind: 'turnDone',
        sessionId: runtime,
        title: 'done',
        projectionAfter: endedAt,
        refreshProjection
      } as never)
      await vi.advanceTimersByTimeAsync(15000)
      expect(refreshProjection).toHaveBeenCalledTimes(1)
      expect(notify).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(30000)
      expect(refreshProjection).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
      vi.useRealTimers()
    }
  })

  it('keeps a failed background read unfinished and retries without duplicate delivery', async () => {
    vi.useFakeTimers()
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    publishSessionState(runtime, createClientSessionState(stored))
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    const refreshProjection = vi
      .fn()
      .mockRejectedValueOnce(new Error('owner offline'))
      .mockImplementation(async () => {
        setSessions([{ id: stored, profile: 'default', kanban_observed_at: Date.now() / 1000 } as never])
      })

    try {
      setSessions([{ id: stored, profile: 'default', kanban_unknown: true } as never])
      dispatchNativeNotification({
        kind: 'turnDone',
        sessionId: runtime,
        title: 'done',
        projectionAfter: endedAt,
        refreshProjection
      })
      await vi.advanceTimersByTimeAsync(15000)
      expect(notify).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(15000)
      expect(refreshProjection).toHaveBeenCalledTimes(2)
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
      vi.useRealTimers()
    }
  })

  it('never releases from a different owner sharing the stored id', () => {
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    publishSessionState(runtime, createClientSessionState(stored))
    recordSessionEventScope({ session_id: runtime, connectionId: 'owner-a', profile: 'work' })
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([{ id: stored, profile: 'work', connection_id: 'owner-b', kanban_observed_at: endedAt } as never])
      expect(
        dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })
      ).toBe(false)
      expect(notify).not.toHaveBeenCalled()
      setSessions([
        {
          id: stored,
          profile: 'work',
          connection_id: 'owner-a',
          kanban_observed_at: endedAt,
          kanban_active: 1
        } as never
      ])
      expect(notify).not.toHaveBeenCalled()
      setSessions([{ id: stored, profile: 'work', connection_id: 'owner-a', kanban_observed_at: endedAt } as never])
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
    }
  })

  it.each(['local', 'owner-a'])('normalizes an unstamped primary row to %s for deferred release', primary => {
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    setPrimaryGatewayConnectionId(primary)
    publishSessionState(runtime, createClientSessionState(stored))
    recordSessionEventScope({ session_id: runtime, connectionId: primary, profile: 'default' })
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([])
      expect(
        dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })
      ).toBe(false)
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: endedAt } as never])
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
      setPrimaryGatewayConnectionId(null)
    }
  })

  it('keeps a proven profile-only event on the primary owner', () => {
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    setPrimaryGatewayConnectionId('owner-a')
    publishSessionState(runtime, createClientSessionState(stored))
    recordSessionEventScope(stampSecondaryProfileOwner({ session_id: runtime } as never, 'work'))
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([{ id: stored, profile: 'work', connection_id: 'owner-b', kanban_observed_at: endedAt } as never])
      expect(
        dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })
      ).toBe(false)
      setSessions([{ id: stored, profile: 'work', kanban_observed_at: endedAt } as never])
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
      setPrimaryGatewayConnectionId(null)
    }
  })

  it('bounds a hung projection refresh and prevents its late publication', async () => {
    vi.useFakeTimers()
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    publishSessionState(runtime, createClientSessionState(stored))
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000
    let oldMayPublish: (() => boolean) | undefined

    const refreshProjection = vi
      .fn()
      .mockImplementationOnce((mayPublish?: () => boolean) => {
        oldMayPublish = mayPublish

        return new Promise<void>(() => {})
      })
      .mockImplementation(async () => {
        setSessions([{ id: stored, profile: 'default', kanban_observed_at: Date.now() / 1000 } as never])
      })

    try {
      setSessions([{ id: stored, profile: 'default', kanban_unknown: true } as never])
      dispatchNativeNotification({
        kind: 'turnDone',
        sessionId: runtime,
        title: 'done',
        projectionAfter: endedAt,
        refreshProjection
      })
      await vi.advanceTimersByTimeAsync(45000)
      expect(refreshProjection).toHaveBeenCalledTimes(2)
      expect(oldMayPublish?.()).toBe(false)
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
      vi.useRealTimers()
    }
  })

  it('retains a missing projection using the independently recorded runtime owner', () => {
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    publishSessionState(runtime, createClientSessionState(stored))
    recordSessionEventScope({ session_id: runtime, connectionId: 'owner-a', profile: 'work' })
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([])
      expect(
        dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })
      ).toBe(false)
      setSessions([{ id: stored, profile: 'work', connection_id: 'owner-a', kanban_observed_at: endedAt } as never])
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
    }
  })

  it('retains deferred completion across a proven same-owner lineage rotation', () => {
    const runtime = freshSession()
    const stored = `${runtime}-old`
    const next = `${runtime}-new`
    publishSessionState(runtime, createClientSessionState(stored))
    recordSessionEventScope({ session_id: runtime, connectionId: 'owner-a', profile: 'work' })
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([
        {
          id: stored,
          profile: 'work',
          connection_id: 'owner-a',
          kanban_observed_at: endedAt,
          kanban_active: 1
        } as never
      ])
      dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })
      setSessions([
        {
          id: next,
          _lineage_ids: [stored, next],
          profile: 'work',
          connection_id: 'owner-a',
          kanban_observed_at: endedAt,
          kanban_active: 1
        } as never
      ])
      publishSessionState(runtime, createClientSessionState(next))
      expect(notify).not.toHaveBeenCalled()
      setSessions([
        {
          id: next,
          _lineage_ids: [stored, next],
          profile: 'work',
          connection_id: 'owner-a',
          kanban_observed_at: endedAt
        } as never
      ])
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      dropSessionState(runtime)
      setSessions([])
    }
  })

  it.each(['unrelated', 'foreign'])('cancels a deferred notice on %s stored-id rotation', reason => {
    const runtime = freshSession()
    const stored = `${runtime}-old`
    const next = `${runtime}-new`
    publishSessionState(runtime, createClientSessionState(stored))
    recordSessionEventScope({ session_id: runtime, connectionId: 'owner-a', profile: 'work' })
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([
        {
          id: stored,
          profile: 'work',
          connection_id: 'owner-a',
          kanban_observed_at: endedAt,
          kanban_active: 1
        } as never
      ])
      dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })
      setSessions([
        {
          id: next,
          _lineage_ids: reason === 'foreign' ? [stored, next] : [next],
          profile: 'work',
          connection_id: reason === 'foreign' ? 'owner-b' : 'owner-a',
          kanban_observed_at: endedAt
        } as never
      ])
      publishSessionState(runtime, createClientSessionState(next))
      setSessions([
        {
          id: next,
          _lineage_ids: [stored, next],
          profile: 'work',
          connection_id: 'owner-a',
          kanban_observed_at: endedAt
        } as never
      ])
      expect(notify).not.toHaveBeenCalled()
    } finally {
      dropSessionState(runtime)
      setSessions([])
    }
  })

  it.each(['new-turn', 'owner-change', 'runtime-gone'])('cancels a deferred completion on %s', reason => {
    const runtime = freshSession()
    const stored = `${runtime}-stored`
    const state = createClientSessionState(stored)
    publishSessionState(runtime, state)
    setActiveSessionId(runtime)
    const endedAt = Date.now() / 1000

    try {
      setSessions([{ id: stored, profile: 'default', kanban_observed_at: endedAt, kanban_active: 1 } as never])
      dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done', projectionAfter: endedAt })

      if (reason === 'new-turn') {
        publishSessionState(runtime, { ...state, busy: true, turnLive: true })
        publishSessionState(runtime, state)
      } else if (reason === 'runtime-gone') {
        dropSessionState(runtime)
        publishSessionState(runtime, state)
      } else {
        setSessions([{ id: stored, profile: 'other', kanban_observed_at: endedAt } as never])
      }

      setSessions([{ id: stored, profile: 'default', kanban_observed_at: endedAt } as never])
      expect(notify).not.toHaveBeenCalled()
    } finally {
      dropSessionState(runtime)
      setSessions([])
    }
  })

  it.each([{ kanban_active: 1 }, { kanban_blocked: 1 }, { kanban_unknown: true }, { kanban_remote_stale: 1 }])(
    'does not announce finished while the origin projection is unresolved: %o',
    stamp => {
      const runtime = freshSession()
      const stored = `${runtime}-stored`
      setActiveSessionId(runtime)
      publishSessionState(runtime, createClientSessionState(stored))

      try {
        setSessions([{ id: stored, ...stamp } as never])
        expect(dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done' })).toBe(false)
        expect(notify).not.toHaveBeenCalled()
        // A later authoritative release is eligible; suppression must not consume
        // the notification throttle. Error/attention paths remain independent.
        setSessions([{ id: stored, kanban_observed_at: Date.now() / 1000 } as never])
        expect(dispatchNativeNotification({ kind: 'turnDone', sessionId: runtime, title: 'done' })).toBe(true)
        expect(notify).toHaveBeenCalledTimes(1)
      } finally {
        dropSessionState(runtime)
        setSessions([])
      }
    }
  )

  it('fires a completion notification for the active session when the window is hidden', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setSessions([{ id: sessionId, kanban_observed_at: Date.now() / 1000 } as never])
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('fires a completion notification when the window is visible but unfocused (alt-tab)', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setSessions([{ id: sessionId, kanban_observed_at: Date.now() / 1000 } as never])
    setWindowState({ focused: false, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a completion notification when the window is focused', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setWindowState({ focused: true, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses a completion notification for a non-active background session (no gateway spam)', () => {
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'turnDone', sessionId: 'busy-bot-session', title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires an attention notification for an off-screen session even when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'background', title: 'approve' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses an attention notification for the active session when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'on-screen', title: 'approve' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires a global completion notification while away with no active session (pet gen)', () => {
    setActiveSessionId(null)
    dispatchNativeNotification({ global: true, kind: 'backgroundDone', title: 'Your pet hatched' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a global notification when the window is focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId(null)
    dispatchNativeNotification({ global: true, kind: 'backgroundDone', title: 'Your pet hatched' })
    expect(notify).not.toHaveBeenCalled()
  })
})

describe('dispatchNativeNotification preferences', () => {
  it('suppresses everything when the master switch is off', () => {
    setNativeNotifyEnabled(false)
    dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses only the disabled kind', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setNativeNotifyKind('turnDone', false)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()

    dispatchNativeNotification({ kind: 'turnError', sessionId, title: 'boom' })
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('dispatchNativeNotification post-connect baseline', () => {
  it('suppresses a prompt replayed right after a socket opens', () => {
    markNativeNotifyBaseline()
    dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires again once the window has passed', () => {
    vi.useFakeTimers()

    try {
      markNativeNotifyBaseline()
      vi.advanceTimersByTime(5000)
      dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('dispatchPluginNativeNotification', () => {
  it('fires while the user is away and tags the plugin id for dedupe', () => {
    dispatchPluginNativeNotification('index-network', { body: 'New match', title: 'Opportunity' })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ body: 'New match', kind: 'plugin', tag: 'index-network', title: 'Opportunity' })
    )
  })

  it('suppresses while the window is focused (the in-app toast covers foreground)', () => {
    setWindowState({ focused: true, hidden: false })
    dispatchPluginNativeNotification('focused-plugin', { title: 'Opportunity' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('is gated by the "plugin" kind preference', () => {
    setNativeNotifyKind('plugin', false)
    dispatchPluginNativeNotification('muted-plugin', { title: 'Opportunity' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('throttles per plugin, so two plugins cannot collapse each other', () => {
    dispatchPluginNativeNotification('plugin-a', { title: 'a' })
    dispatchPluginNativeNotification('plugin-a', { title: 'a again' })
    dispatchPluginNativeNotification('plugin-b', { title: 'b' })
    expect(notify).toHaveBeenCalledTimes(2)
  })

  it('does not register handlers for throttled or suppressed notifications', () => {
    const onActivate = vi.fn()

    // First fires and registers; the immediate repeat is throttled per plugin id.
    dispatchPluginNativeNotification('leak-plugin', { onActivate: () => undefined, title: 'first' })
    dispatchPluginNativeNotification('leak-plugin', { onActivate, title: 'throttled' })
    expect(notify).toHaveBeenCalledTimes(1)

    // The throttled call must not have registered anything: no notifyId ever
    // reached the OS, so its handlers would leak. Invoking with the only
    // minted id (from the first call) must not hit the throttled callback.
    const payload = notify.mock.calls[0]?.[0] as { notifyId?: string }
    invokePluginNotifyActivate(payload.notifyId)
    expect(onActivate).not.toHaveBeenCalled()

    // Fully suppressed (kind disabled): nothing registered either.
    setNativeNotifyKind('plugin', false)
    const suppressed = vi.fn()
    dispatchPluginNativeNotification('other-plugin', { onActivate: suppressed, title: 'muted' })
    expect(notify).toHaveBeenCalledTimes(1)
    invokePluginNotifyActivate(payload.notifyId)
    expect(suppressed).not.toHaveBeenCalled()
  })

  it('forwards icon, resolved activate path, and action buttons (deeplink-compatible)', () => {
    // Unique tag (throttle is per plugin id); activate still uses the plugin deep link.
    dispatchPluginNativeNotification('index-network-alerts', {
      actions: [
        { id: 'open', label: 'Open', activate: 'hermes://index-network/intent/1' },
        { id: 'dismiss', label: 'Dismiss', onAction: () => undefined }
      ],
      activate: 'hermes://index-network/intent/1',
      body: 'New match',
      icon: '/tmp/index-network.png',
      title: 'Opportunity'
    })

    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        activate: '/index-network/intent/1',
        actions: [
          { activate: '/index-network/intent/1', id: 'open', text: 'Open' },
          { activate: undefined, id: 'dismiss', text: 'Dismiss' }
        ],
        icon: '/tmp/index-network.png',
        kind: 'plugin',
        notifyId: expect.stringMatching(/^index-network-alerts:/),
        tag: 'index-network-alerts',
        title: 'Opportunity'
      })
    )
  })

  it('registers onActivate / onAction handlers keyed by notifyId', () => {
    const onActivate = vi.fn()
    const onAction = vi.fn()

    dispatchPluginNativeNotification('handlers-plugin', {
      activate: 'hermes://index-network/intent/1',
      onActivate,
      actions: [{ id: 'dismiss', label: 'Dismiss', onAction }],
      title: 'Opportunity'
    })

    const payload = notify.mock.calls[0]?.[0] as { notifyId?: string }
    expect(payload.notifyId).toBeTruthy()
    invokePluginNotifyActivate(payload.notifyId)
    expect(onActivate).toHaveBeenCalledTimes(1)
    expect(invokePluginNotifyAction(payload.notifyId, 'dismiss')).toBe(true)
    expect(onAction).toHaveBeenCalledTimes(1)
  })
})

describe('dispatchNativeNotification throttle', () => {
  it('collapses duplicate kind+session within the throttle window', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setSessions([{ id: sessionId, kanban_observed_at: Date.now() / 1000 } as never])
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done again' })
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('sendTestNativeNotification', () => {
  it('fires regardless of focus or active session', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    sendTestNativeNotification('Hermes', 'works')
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('respondToApprovalAction', () => {
  const request = vi.fn().mockResolvedValue({ resolved: true })

  beforeEach(() => {
    clearAllPrompts()
    request.mockClear()
    $gateway.set({ request } as unknown as ReturnType<typeof $gateway.get>)
  })

  afterEach(() => {
    $gateway.set(null)
  })

  it('approves via approval.respond {choice: "once"} and clears the prompt', async () => {
    setActiveSessionId('bg')
    setApprovalRequest({ command: 'rm -rf /', description: 'dangerous', sessionId: 'bg' })

    await respondToApprovalAction('bg', 'approve')

    expect(request).toHaveBeenCalledWith(
      'approval.respond',
      { all: false, choice: 'once', session_id: 'bg' },
      // #55433: the respond RPC carries an explicit deadline covering the backend's approvals window.
      APPROVAL_RESPOND_REQUEST_TIMEOUT_MS,
      undefined
    )
    expect($approvalRequest.get()).toBeNull()
  })

  it('answers the exact notification request without clearing the rest of its stack', async () => {
    setActiveSessionId('bg')
    setApprovalRequest({ command: 'first', description: 'first', requestId: 'r1', sessionId: 'bg' })
    setApprovalRequest({ command: 'second', description: 'second', requestId: 'r2', sessionId: 'bg' })
    await respondToApprovalAction('bg', 'approve:r1')
    expect(request).toHaveBeenCalledWith(
      'approval.respond',
      {
        all: false,
        choice: 'once',
        request_id: 'r1',
        session_id: 'bg'
      },
      // #55433: the respond RPC carries an explicit deadline covering the backend's approvals window.
      APPROVAL_RESPOND_REQUEST_TIMEOUT_MS,
      undefined
    )
    expect($approvalRequest.get()?.requestId).toBe('r2')
    await respondToApprovalAction('bg', 'approve:r1')
    expect($approvalRequest.get()?.requestId).toBe('r2')
  })

  it('rejects via approval.respond {choice: "deny"}', async () => {
    await respondToApprovalAction('bg', 'reject')
    expect(request).toHaveBeenCalledWith(
      'approval.respond',
      { all: false, choice: 'deny', session_id: 'bg' },
      // #55433: the respond RPC carries an explicit deadline covering the backend's approvals window.
      APPROVAL_RESPOND_REQUEST_TIMEOUT_MS,
      undefined
    )
  })

  it('ignores unknown action ids', async () => {
    await respondToApprovalAction('bg', 'snooze')
    expect(request).not.toHaveBeenCalled()
  })

  it('no-ops without a gateway', async () => {
    $gateway.set(null)
    await respondToApprovalAction('bg', 'approve')
    expect(request).not.toHaveBeenCalled()
  })

  it('does not retry an approval action for a runtime already marked gone', async () => {
    markSessionGone('bg')

    await respondToApprovalAction('bg', 'approve')

    expect(request).not.toHaveBeenCalled()
  })
})

describe('app sounds mute', () => {
  afterEach(() => {
    setHapticsMuted(false)
    setSessions([])
  })

  const fire = () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    // Fork: a turnDone needs a fresh kanban projection showing no open work.
    setSessions([{ id: sessionId, kanban_observed_at: Date.now() / 1000 } as never])
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    dispatchPluginNativeNotification(freshSession(), { title: 'Opportunity' })

    return notify.mock.calls.map(([payload]) => payload.silent)
  }

  it('sends OS notifications silently while app sounds are muted', () => {
    setHapticsMuted(true)
    expect(fire()).toEqual([true, true])
  })

  it('leaves OS notification sound alone when not muted', () => {
    expect(fire()).toEqual([undefined, undefined])
  })
})
