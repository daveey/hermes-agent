/**
 * [daveey] Left-pane "Blocked" section: every blocked card on every board and
 * gateway, between Pinned and Projects (core's `sidebar.sections` area). This
 * replaces the in-app "task blocked" toast — the card stays visible here until
 * someone unblocks it, instead of a popup that vanishes. Clicking a row opens
 * the task's drawer on its own board. Hidden while nothing is blocked.
 */

import { atom, host, SidebarSection, SidebarSectionRow, useQuery, useValue } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { blockedKey, fetchBlockedTasks, useKanbanScope } from './api'
import { $openTaskRequest } from './open-task'
import { ago, useKanban } from './ui'

/** Collapsed? Persisted by plugin.tsx through ctx.storage. */
export const $blockedSectionOpen = atom<boolean>(true)

const INITIAL_ROWS = 5
const LOAD_STEP = 10
const POLL_MS = 30_000

export function BlockedTasksSection() {
  const k = useKanban()
  const scope = useKanbanScope()
  const open = useValue($blockedSectionOpen)
  const [visible, setVisible] = useState(INITIAL_ROWS)

  // Socket frames invalidate this key (api.ts onEventsFrame); the poll covers
  // boards without an open socket and the other gateways.
  const { data: tasks } = useQuery({
    queryFn: fetchBlockedTasks,
    queryKey: blockedKey(scope),
    refetchInterval: POLL_MS
  })

  if (!tasks?.length) {
    return null
  }

  const shown = tasks.slice(0, visible)

  return (
    <SidebarSection
      count={tasks.length}
      hiddenCount={tasks.length - shown.length}
      label={k.sidebar.blocked}
      loadMoreStep={LOAD_STEP}
      onLoadMore={() => setVisible(n => n + LOAD_STEP)}
      onToggle={() => $blockedSectionOpen.set(!open)}
      open={open}
    >
      {shown.map(task => (
        <SidebarSectionRow
          accentColor="var(--ui-yellow)"
          icon="question"
          key={`${task.gateway ?? ''}:${task.board}:${task.id}`}
          label={task.title || task.id}
          meta={ago(task.created_at) ?? undefined}
          onSelect={() => {
            $openTaskRequest.set({ board: task.board, taskId: task.id })
            host.navigate('/kanban')
          }}
          tip={k.sidebar.blockedTip(task.title || task.id, (task.latest_summary ?? '').trim())}
        />
      ))}
    </SidebarSection>
  )
}
