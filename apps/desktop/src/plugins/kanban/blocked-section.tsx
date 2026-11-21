/**
 * [daveey] Left-pane "Blocked" section: every blocked card on every board and
 * gateway, between Pinned and Projects (core's `sidebar.sections` area). This
 * replaces the in-app "task blocked" toast — the card stays visible here until
 * someone unblocks it, instead of a popup that vanishes. Cards are grouped
 * under the chat managing them (header click opens that chat on its own
 * gateway); a card row
 * opens the task's drawer on its own board. Hidden while nothing is blocked.
 */

import {
  atom,
  host,
  type PluginGateway,
  type PluginProfileRoute,
  SidebarSection,
  SidebarSectionRow,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { Fragment, useState } from 'react'

import { blockedKey, type BlockedTask, boardGateways, fetchBlockedTasks, useKanbanScope } from './api'
import { $openTaskRequest } from './open-task'
import { ago, useKanban } from './ui'

/** Collapsed? Persisted by plugin.tsx through ctx.storage. */
export const $blockedSectionOpen = atom<boolean>(true)

const INITIAL_ROWS = 5
const LOAD_STEP = 10
const POLL_MS = 30_000

export interface BlockedGroup {
  key: string
  /** Managing chat's live stored id; null = no session owns these cards. */
  sessionId: null | string
  title: null | string
  creator?: null | string
  gateway?: string
  /** Hermes profile the session lives in on its gateway. */
  profile: null | string
  tasks: BlockedTask[]
}

/** How to open a group's chat: null = on the routed gateway (plain open),
 *  else the owner route that dials the session's own gateway + profile — the
 *  same owner a cross-gateway sidebar row resumes through. */
export function managingSessionRoute(
  group: Pick<BlockedGroup, 'gateway' | 'profile'>,
  routedGateway: string,
  gateways: readonly PluginGateway[]
): null | PluginProfileRoute {
  if (!group.gateway || group.gateway === routedGateway) {
    return null
  }

  const profile = group.profile || 'default'
  const kind = gateways.find(gateway => gateway.id === group.gateway)?.kind
  const mode = kind === 'local' ? ('local' as const) : ('remote' as const)

  return { connectionId: group.gateway, mode, profile, targetProfile: profile }
}

/** Cards grouped by the chat managing them, in the order the fetch ranked
 *  them (a group sits where its most urgent card would), sessionless last. */
export function groupBlockedTasks(tasks: readonly BlockedTask[]): BlockedGroup[] {
  const groups = new Map<string, BlockedGroup>()

  for (const task of tasks) {
    const sessionId = task.manager?.id ?? null

    const key = sessionId
      ? `${task.gateway ?? ''}:${sessionId}`
      : JSON.stringify([task.gateway ?? '', 'creator', task.creator ?? null])

    let group = groups.get(key)

    if (!group) {
      group = {
        creator: task.creator,
        gateway: task.gateway,
        key,
        profile: task.manager?.profile ?? null,
        sessionId,
        tasks: [],
        title: task.manager?.title ?? null
      }
      groups.set(key, group)
    }

    group.title ||= task.manager?.title ?? null
    group.tasks.push(task)
  }

  const all = [...groups.values()]

  return [...all.filter(g => g.sessionId), ...all.filter(g => !g.sessionId)]
}

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

  const groups = groupBlockedTasks(tasks)
  const routedGateway = boardGateways()[0]
  let budget = visible

  const openTask = (task: BlockedTask) => {
    $openTaskRequest.set({ board: task.board, taskId: task.id })
    host.navigate('/kanban')
  }

  return (
    <SidebarSection
      count={tasks.length}
      hiddenCount={Math.max(0, tasks.length - visible)}
      label={k.sidebar.blocked}
      loadMoreStep={LOAD_STEP}
      onLoadMore={() => setVisible(n => n + LOAD_STEP)}
      onToggle={() => $blockedSectionOpen.set(!open)}
      open={open}
    >
      {groups.map(group => {
        if (budget <= 0) {
          return null
        }

        const shown = group.tasks.slice(0, budget)
        budget -= shown.length

        const creatorTitle = group.creator
          ? [group.creator, group.gateway].filter(Boolean).join(' · ')
          : k.sidebar.noSession

        const title = group.sessionId ? group.title || k.sidebar.untitledSession : creatorTitle
        const sessionId = group.sessionId

        return (
          <Fragment key={group.key}>
            <SidebarSectionRow
              icon={sessionId ? 'comment-discussion' : 'circle-slash'}
              label={title}
              meta={group.tasks.length}
              muted
              onSelect={() => {
                if (!sessionId) {
                  openTask(group.tasks[0])

                  return
                }

                const route = managingSessionRoute(group, routedGateway, host.state.gateways?.get() ?? [])
                void host.openSession(sessionId, route ? { route } : {})
              }}
              tip={sessionId ? k.sidebar.openSession(title) : title}
            />
            {shown.map(task => (
              <SidebarSectionRow
                accentColor="var(--ui-yellow)"
                icon="question"
                indent
                key={`${task.gateway ?? ''}:${task.board}:${task.id}`}
                label={task.title || task.id}
                meta={ago(task.created_at) ?? undefined}
                onSelect={() => openTask(task)}
                tip={k.sidebar.blockedTip(task.title || task.id, (task.latest_summary ?? '').trim())}
              />
            ))}
          </Fragment>
        )
      })}
    </SidebarSection>
  )
}
