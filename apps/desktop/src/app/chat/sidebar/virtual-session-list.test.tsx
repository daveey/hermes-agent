import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type { SidebarListRow } from '@/lib/session-date-groups'
import { $sessionListDensity } from '@/store/session-list-density'

import { SESSION_CARD_ROW_ESTIMATE_PX, sessionRowEstimate } from './session-row-details'
import { VirtualSessionList } from './virtual-session-list'

// The virtualizer is mocked with a STABLE instance (the real hook returns
// one), so the component's measure() effect fires exactly when its deps
// change — which is the behavior under test: `card` must invalidate cached
// measurements, or toggling Inbox style leaves the previous mode's row
// heights in place (#88473).
const measureSpy = vi.fn()
let estimateSize: (index: number) => number = () => 0

const stableVirtualizer = {
  measure: (...args: []) => measureSpy(...args),
  getVirtualItems: vi.fn((): Array<{ index: number; start: number }> => []),
  getTotalSize: () => 0,
  measureElement: vi.fn()
}

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: (options: { estimateSize: (index: number) => number }) => {
    estimateSize = options.estimateSize

    return stableVirtualizer
  }
}))

vi.mock('./chrome', () => ({ SidebarDateDivider: () => null }))
const capturedRows = vi.hoisted(() => vi.fn())
vi.mock('./session-row', () => ({
  SidebarSessionRow: (props: unknown) => {
    capturedRows(props)

    return null
  }
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: { sidebar: { dateDivider: {} } } })
}))

const session = (id: string) =>
  ({ archived: false, id, last_active: 0, profile: 'default', started_at: 0 }) as unknown as SessionInfo

const rows: SidebarListRow[] = [
  { key: 'today', kind: 'divider', label: 'Today' },
  { entry: { session: session('s1') }, kind: 'session' },
  { entry: { session: session('s2') }, kind: 'session' }
]

const defaultProps = {
  activeSessionId: null,
  onDeleteSession: () => {},
  onResumeSession: () => {},
  onArchiveSession: () => {},
  onTogglePin: () => {},
  onToggleUnread: () => {},
  pinned: false,
  rows,
  sortable: false
}

function renderList(props: Partial<Parameters<typeof VirtualSessionList>[0]> = {}) {
  return render(<VirtualSessionList {...defaultProps} {...props} />)
}

describe('VirtualSessionList row measurement', () => {
  beforeEach(() => {
    measureSpy.mockClear()
    stableVirtualizer.getVirtualItems.mockReturnValue([])
    capturedRows.mockClear()
    $sessionListDensity.set('compact')
  })

  afterEach(() => {
    cleanup()
  })

  it('re-measures when the density changes', () => {
    const { rerender } = renderList()

    const afterMount = measureSpy.mock.calls.length

    $sessionListDensity.set('detailed')
    rerender(<VirtualSessionList {...defaultProps} />)

    expect(measureSpy.mock.calls.length).toBeGreaterThan(afterMount)
  })

  it('re-measures when Inbox card mode toggles — stale compact measurements must not survive the switch (#88473)', () => {
    const { rerender } = renderList()

    const afterMount = measureSpy.mock.calls.length

    rerender(<VirtualSessionList {...defaultProps} card />)

    expect(measureSpy.mock.calls.length).toBeGreaterThan(afterMount)
  })

  it('routes the estimate by row kind and mode', () => {
    const { rerender } = renderList()

    // Dividers keep their own fixed estimate in every mode.
    expect(estimateSize(0)).toBe(28)
    expect(estimateSize(1)).toBe(sessionRowEstimate('compact'))

    rerender(<VirtualSessionList {...defaultProps} card />)

    expect(estimateSize(0)).toBe(28)
    expect(estimateSize(1)).toBe(SESSION_CARD_ROW_ESTIMATE_PX)
  })

  it('estimates a card at or above the tallest four-line card stack (#88473)', () => {
    // A full Inbox card renders four text lines (header, title, preview,
    // model/size) where the tallest inline density renders three — plus the
    // card's own padding, and one more title line when the title wraps on a
    // narrow sidebar. The estimate must cover that worst case: undersized
    // estimates paint rows over their neighbours on cold start, before
    // self-measurement can correct them.
    const onePreviewLine = 13.5
    const oneTitleLine = 17.6

    expect(SESSION_CARD_ROW_ESTIMATE_PX).toBeGreaterThanOrEqual(
      sessionRowEstimate('detailed') + onePreviewLine + oneTitleLine
    )
  })
})

it('forwards the full owner through a virtualized row archive gesture', () => {
  stableVirtualizer.getVirtualItems.mockReturnValue([{ index: 0, start: 0 }])
  capturedRows.mockClear()
  const selected = { ...session('same-id'), connection_id: 'conn-mini', profile: 'work', offline_cached: true }
  const onArchiveSession = vi.fn()
  renderList({ rows: [{ kind: 'session', entry: { session: selected } }], onArchiveSession })
  capturedRows.mock.calls[0][0].onArchive()
  expect(onArchiveSession).toHaveBeenCalledWith('same-id', selected)
})
