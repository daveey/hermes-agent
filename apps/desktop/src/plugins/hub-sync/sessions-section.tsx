/**
 * [daveey] Left-pane "Synced" section: sessions on gateways that are not live
 * right now (unreachable, or not registered on this Desktop), read from the
 * window gateway's hermes-hub replica. Rows are greyed with a "synced Xm ago"
 * hint; archive / pin / rename queue an owner request through hub-sync (works
 * with the hub down, sent when it's back) and show a pending marker until the
 * owner applies it. Live rows stay primary: a gateway whose rows are in the
 * live sidebar never appears here. Hidden when there is nothing to show.
 */
import {
  atom,
  Codicon,
  host,
  SidebarSection,
  SidebarSectionRow,
  Tip,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { type KeyboardEvent, type MouseEvent, useState } from 'react'

import { loadReplicaSidebarRows, replicaRowRoute, sendSessionAction } from './api'
import { type HubSessionAction, type HubSessionRow, isRowPending, markRowPending, syncedAgo } from './model'

export const $hubSessionsOpen = atom<boolean>(true)

const INITIAL_ROWS = 8
const LOAD_STEP = 10
const POLL_MS = 60_000

const rowKey = (row: HubSessionRow) => `${row.gateway}\u0000${row.key}`

function ActionButton({ icon, label, onClick }: { icon: string; label: string; onClick: () => void }) {
  return (
    <Tip label={label}>
      <button
        aria-label={label}
        className="flex size-5 items-center justify-center rounded text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground"
        onClick={(event: MouseEvent) => {
          event.stopPropagation()
          onClick()
        }}
        type="button"
      >
        <Codicon name={icon} size="0.75rem" />
      </button>
    </Tip>
  )
}

export function HubSessionsSection() {
  const live = useValue(host.sessions.rows)
  const open = useValue($hubSessionsOpen)
  const [visible, setVisible] = useState(INITIAL_ROWS)
  const [optimistic, setOptimistic] = useState<Record<string, HubSessionRow>>({})
  const [renaming, setRenaming] = useState<null | string>(null)
  const [draft, setDraft] = useState('')

  const liveIds = live.map(row => `${row.connection_id ?? ''}:${row.id}`).join(',')

  const { data: rows } = useQuery({
    queryFn: () => loadReplicaSidebarRows(live),
    queryKey: ['hub-sync', 'sidebar-sessions', liveIds],
    refetchInterval: POLL_MS
  })

  if (!rows?.length) {
    return null
  }

  const shown = rows.map(row => optimistic[rowKey(row)] ?? row)

  const act = async (row: HubSessionRow, action: HubSessionAction, args?: Record<string, unknown>) => {
    try {
      const result = await sendSessionAction(row, action, args)
      const [next] = markRowPending([row], row, { action, id: result.id, state: result.state })
      setOptimistic(prev => ({ ...prev, [rowKey(row)]: { ...next, ...(action === 'rename' ? { title: String(args?.title ?? '') } : {}) } }))
    } catch (error) {
      console.warn('[hub-sync] owner request failed', error)
    }
  }

  const commitRename = (row: HubSessionRow) => {
    const title = draft.trim()
    setRenaming(null)

    if (title && title !== (row.title ?? '')) {
      void act(row, 'rename', { title })
    }
  }

  return (
    <SidebarSection
      count={shown.length}
      hiddenCount={Math.max(0, shown.length - visible)}
      label="Synced from other gateways"
      loadMoreStep={LOAD_STEP}
      onLoadMore={() => setVisible(n => n + LOAD_STEP)}
      onToggle={() => $hubSessionsOpen.set(!open)}
      open={open}
    >
      {shown.slice(0, visible).map(row => {
        const key = rowKey(row)
        const label = row.title?.trim() || row.id
        const pending = isRowPending(row)
        const age = syncedAgo(row.synced_at)
        const route = replicaRowRoute(row)

        if (renaming === key) {
          return (
            <input
              aria-label={`New title for ${label}`}
              autoFocus
              className="mx-2 my-px h-6 rounded border border-(--ui-border) bg-transparent px-1.5 text-xs"
              key={key}
              onBlur={() => commitRename(row)}
              onChange={event => setDraft(event.target.value)}
              onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
                if (event.key === 'Enter') {
                  commitRename(row)
                } else if (event.key === 'Escape') {
                  setRenaming(null)
                }
              }}
              value={draft}
            />
          )
        }

        return (
          <SidebarSectionRow
            actions={
              <>
                <ActionButton
                  icon={row.pinned ? 'pinned' : 'pin'}
                  label={`${row.pinned ? 'Unpin' : 'Pin'} on ${row.gateway} (via hub)`}
                  onClick={() => void act(row, row.pinned ? 'unpin' : 'pin')}
                />
                <ActionButton
                  icon="edit"
                  label={`Rename on ${row.gateway} (via hub)`}
                  onClick={() => {
                    setDraft(label)
                    setRenaming(key)
                  }}
                />
                <ActionButton
                  icon="archive"
                  label={`Archive on ${row.gateway} (via hub)`}
                  onClick={() => void act(row, 'archive')}
                />
              </>
            }
            icon={pending ? 'sync' : 'cloud'}
            key={key}
            label={label}
            meta={pending ? 'pending' : age}
            muted
            onSelect={() => {
              if (route) {
                void host.openSession(row.id, { route })
              }
            }}
            tip={`${label} — ${row.gateway} is not reachable; ${age}${pending ? ' · change pending' : ''}${route ? '' : ' · not registered on this Desktop'}`}
          />
        )
      })}
    </SidebarSection>
  )
}
