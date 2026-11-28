import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { produceSessionStateV2 } from './session-state-v2-producer'

// Substitute transport/runtime inputs, never the status classifier or consumer under test.
vi.mock('@/store/session', async () => {
  const { atom } = await import('nanostores')
  const sessions = atom<any[]>([])

  return {
    $sessions: sessions,
    $messagingSessions: atom([]),
    $cronSessions: atom([]),
    $unlistedSessionOwnerRows: atom([]),
    $unreadFinishedSessionIds: atom([]),
    $activeSessionId: atom('fixture-chat'),
    lineageAliases: (id: string) => [id],
    sessionMatchesStoredId: (row: any, id: string) => row.id === id,
    ownerLookupSessionRows: () => sessions.get()
  }
})
vi.mock('@/store/session-states', async () => {
  const { atom } = await import('nanostores')

  return {
    $sessionStates: atom({}),
    $sessionTiles: atom([]),
    $attentionSessionIds: atom([]),
    $draftSessionIds: atom([]),
    $stalledSessionIds: atom([]),
    $workingSessionIds: atom([]),
    holdsTurnClaim: () => false,
    knownOwnerForSession: () => undefined,
    runtimeSessionOwner: () => undefined,
    storedSessionIdForRuntimeId: (id: string) => id
  }
})
vi.mock('@/store/composer-status', async () => {
  const { atom } = await import('nanostores')

  return {
    $backgroundRunningSessionIds: atom([]),
    $backgroundStatusBySession: atom({})
  }
})
vi.mock('@/store/subagents', async () => ({ $subagentsBySession: (await import('nanostores')).atom({}) }))
vi.mock('@/store/project-tree', async () => ({ $projectTree: (await import('nanostores')).atom([]) }))
vi.mock('@/store/owner-queue-reconcile', async () => ({
  $pausedQueueSessionIds: (await import('nanostores')).atom([])
}))
vi.mock('@/store/session-unread-remote', async () => ({
  $unreadWriteGuard: (await import('nanostores')).atom(new Map()),
  UNREAD_WRITE_GUARD_MS: 1000
}))
vi.mock('@/store/gateway', async () => ({
  $gateway: (await import('nanostores')).atom(null),
  $activeGatewayRoute: (await import('nanostores')).atom({}),
  activeGatewayProfileKey: () => 'default',
  gatewayRouteState: () => ({ key: 'local' }),
  isActivePrimary: () => true,
  primaryGatewayConnectionId: () => 'local'
}))
vi.mock('@/store/session-request-router', () => ({ isSessionOwnerRoute: () => false }))
vi.mock('@/store/committed-stripe-report', () => ({ useCommittedStripe: () => undefined }))
vi.mock('@/store/desktop-metrics', () => ({ recordFeatureToggle: () => undefined }))
vi.mock('@/store/prompts', () => ({
  answerApproval: vi.fn(),
  clearApprovalRequest: vi.fn(),
  replayPendingApproval: vi.fn(),
  sessionApprovalRequest: vi.fn(),
  sessionApprovalRequests: vi.fn()
}))
vi.mock('@/store/runtime-gone', () => ({
  isSessionGone: () => false,
  isSessionGoneForBackgroundPolling: () => false,
  markSessionGone: vi.fn()
}))
vi.mock('@/i18n', () => ({ translateNow: (key: string, name: string) => `${key}:${name}` }))
vi.mock('@/components/pane-shell/workspace-scope', async () => ({
  $workspaceOwnerLabels: (await import('nanostores')).atom({}),
  workspaceOwnerTitle: (title: string) => title
}))
vi.mock('@/store/profile', () => ({ normalizeProfileKey: (profile: string) => profile || 'default' }))
vi.mock('@/store/session-focus', async () => ({
  $focusedStoredSessionId: (await import('nanostores')).atom('fixture-chat')
}))
vi.mock('@/lib/host-capabilities', () => ({ hostHas: () => true }))
vi.mock('@/plugins/kanban/completion-notify', () => ({
  bindCompletionNotify: () => () => {},
  onKanbanEventsFrame: () => {}
}))
vi.mock('@/plugins/kanban/comment-route', () => ({ commentOnOwner: vi.fn() }))
const view = vi.hoisted(() => ({ tasks: [] as any[], rows: [] as any[], queries: [] as any[] }))
vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return {
    atom,
    host: {
      activeConnectionId: () => 'local',
      state: { connectionId: atom('local'), gateways: atom([{ id: 'local', kind: 'local' }]) },
      navigate: vi.fn(),
      openSession: vi.fn()
    },
    captureGatewayFileDownload: vi.fn(),
    hubGatewayForConnection: () => null,
    hubGatewayTasks: vi.fn(),
    hubRequest: vi.fn(),
    learnHubGatewayName: vi.fn(),
    queryClient: new (await import('@tanstack/react-query')).QueryClient(),
    useValue: (store: any) => store.get(),
    useQuery: (query: any) => {
      view.queries.push(query)

      return { data: view.tasks }
    },
    SidebarSection: ({ children, count }: any) => createElement('section', { 'data-count': count }, children),
    SidebarSectionRow: (props: any) => {
      view.rows.push(props)

      return createElement('div', { 'data-icon': props.icon }, props.label)
    }
  }
})
vi.mock('@/plugins/kanban/ui', () => ({
  ago: () => null,
  useKanban: () => ({
    sidebar: {
      blocked: 'Blocked',
      noSession: 'No managing session',
      untitledSession: 'Untitled session',
      openSession: (s: string) => s,
      blockedTip: (s: string) => s
    }
  })
}))

import { SessionTabAccent } from '@/app/chat/session-tab-accent'
import { bindApi, fetchBlockedTasks, resetGatewayCooldowns } from '@/plugins/kanban/api'
import { BlockedTasksSection, groupBlockedTasks } from '@/plugins/kanban/blocked-section'
import { $tabFlashText, flagTabAttention } from '@/store/browser-tab-attention'
import { installBrowserTabTitle } from '@/store/browser-tab-title'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { __resetNativeNotifyBaselineForTests } from '@/store/notify-baseline'
import { $sessions } from '@/store/session'
import { $sessionDotStateById, sessionStatusStripe } from '@/store/session-dot-state'

const scenarios: Array<{ id: string }> = JSON.parse(
  readFileSync(resolve(import.meta.dirname, '../../../../tests/fixtures/session-state-v2/scenarios.json'), 'utf8')
).scenarios
let produced: ReturnType<typeof produceSessionStateV2>
beforeAll(() => {
  produced = produceSessionStateV2()
}, 90_000)

// Reviewed expectations: dot, canonical stripe, turnDone dispatch, Blocked section count.
const expected: Record<string, [string, string, boolean, number]> = {
  running_card: ['background', 'waiting', false, 0],
  human_brief: ['background', 'stuck', false, 1],
  human_no_brief: ['background', 'stuck', false, 1],
  waiting_card: ['background', 'external', false, 0],
  waiting_ci: ['background', 'waiting', false, 0],
  waiting_clock: ['background', 'waiting', false, 0],
  waiting_deploy: ['background', 'waiting', false, 0],
  triage_599: ['background', 'waiting', false, 0],
  triage_600: ['background', 'waiting', false, 0],
  triage_601: ['background', 'stuck', false, 0],
  stale_replica: ['background', 'attention', false, 0],
  unblocked_after_block: ['background', 'waiting', false, 0],
  reblocked: ['background', 'stuck', false, 1],
  done: ['idle', 'done', true, 0],
  subagent_running: ['background', 'waiting', true, 0],
  process_running: ['background', 'waiting', true, 0],
  turn_failed: ['idle', 'attention', true, 0]
}

// Important pinned producer semantics: the card prerequisite is external, triage only
// escalates strictly after 600 s, and failed-turn presentation is amber attention.
// turnDone currently checks card release only: running processes/children and failed
// turns still pass this guard. These goldens expose that limitation, not an endorsement.
let dispose = () => {}

let uninstallTitle = () => {}
const initialDesktop = window.hermesDesktop
afterEach(() => {
  dispose()
  resetGatewayCooldowns()
  $sessions.set([])
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => true })
  window.dispatchEvent(new Event('focus'))
  uninstallTitle()
  window.hermesDesktop = initialDesktop
  vi.useRealTimers()
})

describe('Python producer → Desktop golden surfaces', () => {
  it('covers the exact shared scenario set', () => {
    expect(produced.cases.map((c: any) => c.id).sort()).toEqual(Object.keys(expected).sort())
    expect(produced.cases).toHaveLength(17)
  })
  it.each(scenarios)(
    '$id: dot/stripe, tab accent, completion dispatch and blocked fetch/render',
    async (scenario: { id: string }) => {
      const c = produced.cases.find((item: { id: string }) => item.id === scenario.id)
      expect(c).toBeDefined()
      vi.useFakeTimers()
      vi.setSystemTime(produced.now * 1000)
      const [dot, stripe, notifyExpected, blockedCount] = expected[c.id]
      // Python row and outcome are consumed intact, not normalized into synthetic status.
      $sessions.set([c.session_row])
      expect($sessionDotStateById.get()[c.session_row.id] ?? 'idle').toBe(dot)
      expect(sessionStatusStripe(dot as never, true, false, 0, false, null, 0, 0, 0, c.outcome)).toBe(stripe)
      const markup = renderToStaticMarkup(
        createElement(SessionTabAccent, {
          session: c.session_row,
          storedSessionId: c.session_row.id,
          children: 'Fixture'
        })
      )
      expect(markup).toContain(`data-session-tab-accent="${stripe}"`)
      const notify = vi.fn().mockResolvedValue(true)
      window.hermesDesktop = { notify } as never
      Object.defineProperty(document, 'hidden', { configurable: true, value: true })
      Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => false })
      __resetNativeNotifyBaselineForTests()
      // Separate scenario time prevents cross-case throttle; producer timestamps stay unchanged.
      vi.setSystemTime((produced.now + produced.cases.indexOf(c) * 2) * 1000)
      expect(dispatchNativeNotification({ kind: 'turnDone', sessionId: c.session_row.id, title: c.id })).toBe(
        notifyExpected
      )
      expect(notify).toHaveBeenCalledTimes(notifyExpected ? 1 : 0)

      if (notifyExpected) {
        expect(notify).toHaveBeenCalledWith(
          expect.objectContaining({
            kind: 'turnDone',
            sessionId: 'fixture-chat',
            focusSessionId: 'fixture-chat',
            title: c.id
          })
        )
      }

      // Real browser attention consumer exercised only after a notification is accepted.
      uninstallTitle = installBrowserTabTitle()

      if (notifyExpected) {
        flagTabAttention(c.id)
      }
      expect($tabFlashText.get()).toBe(notifyExpected ? c.id : null)
      expect(document.title).toBe(notifyExpected ? c.id : 'Hermes')

      if (notifyExpected) {
        vi.advanceTimersByTime(1000)
        expect(document.title).toBe('Hermes')
        vi.advanceTimersByTime(1000)
        expect(document.title).toBe(c.id)
      }

      const blocked = c.cards.filter((card: any) => card.status === 'blocked')
      const calls: string[] = []
      dispose = bindApi(
        (async (path: string) => {
          calls.push(path)

          if (path === '/boards') {
            return { boards: [{ slug: 'default', counts: { blocked: blocked.length } }], current: 'default' }
          }

          if (path.startsWith('/board?')) {
            return { columns: [{ name: 'blocked', tasks: blocked }], latest_event_id: 0 }
          }

          if (path.startsWith('/session-labels?')) {
            return {
              profile: 'default',
              sessions: { 'fixture-chat': { tip_id: 'fixture-chat', title: 'Fixture chat' } }
            }
          }
          throw new Error(`unexpected transport ${path}`)
        }) as never,
        { get: (_: string, fallback: any) => fallback, set: vi.fn(), remove: vi.fn() },
        (() => () => {}) as never
      )
      view.tasks = await fetchBlockedTasks()
      view.rows = []
      view.queries = []
      expect(view.tasks).toHaveLength(blockedCount)
      expect(groupBlockedTasks(view.tasks).map(g => [g.sessionId, g.tasks.length])).toEqual(
        blockedCount ? [['fixture-chat', 1]] : []
      )
      const blockedMarkup = renderToStaticMarkup(createElement(BlockedTasksSection))
      expect(view.queries[0].queryFn).toBe(fetchBlockedTasks)
      expect(view.rows.map(r => [r.icon, r.label])).toEqual(
        blockedCount
          ? [
              ['comment-discussion', 'Fixture chat'],
              ['question', 'Fixture work']
            ]
          : []
      )
      expect(Boolean(blockedMarkup)).toBe(blockedCount > 0)
      expect(calls).toContain('/boards')
    }
  )
})
