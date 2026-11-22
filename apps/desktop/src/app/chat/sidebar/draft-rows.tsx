import { useStore } from '@nanostores/react'
import { useLocation } from 'react-router'

import { NEW_CHAT_ROUTE } from '@/app/routes'
import { $mainDraftSessionCwd } from '@/app/session/new-session-cwd'
import { noteActiveTreeGroup, revealTreePane } from '@/components/pane-shell/tree/store'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $draftTitles, $freshDraftKey, takeSessionDraft } from '@/store/composer'
import { $projects } from '@/store/projects'
import {
  $activeSessionId,
  $freshDraftReady,
  $selectedStoredSessionId,
  $sessions,
  $unlistedSessionOwnerRows
} from '@/store/session'
import { $focusedSessionIsTile, $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionTiles, focusOpenSession } from '@/store/session-states'

import { sessionBucketId, sessionMatchesProjectFilter } from './projects/workspace-groups'

interface DraftRow {
  id: string
  cwd: null | string
  git_repo_root?: string | null
  selected: boolean
  main: boolean
}

/** Presentation only: drafts never enter $sessions, project snapshots, or storage. */
export function SidebarDraftRows({
  projectId,
  filter = [],
  archived = false
}: {
  projectId?: string
  filter?: readonly string[]
  archived?: boolean
}) {
  const { t } = useI18n()
  const { pathname } = useLocation()
  const ready = useStore($freshDraftReady)
  const runtime = useStore($activeSessionId)
  const selected = useStore($selectedStoredSessionId)
  const focused = useStore($focusedStoredSessionId)
  const tileFocused = useStore($focusedSessionIsTile)
  const cwd = useStore($mainDraftSessionCwd)
  const key = useStore($freshDraftKey)
  const titles = useStore($draftTitles)
  const sessions = useStore($sessions)
  const stubs = useStore($unlistedSessionOwnerRows)
  const tiles = useStore($sessionTiles)
  const projects = useStore($projects)
  const rows = new Map<string, DraftRow>()

  if (ready && !runtime && !selected && pathname === NEW_CHAT_ROUTE) {
    rows.set(key, { cwd: cwd || null, id: key, main: true, selected: !tileFocused })
  }

  for (const stub of stubs) {
    const tile = tiles.find(candidate => candidate.storedSessionId === stub.id)
    const draft = takeSessionDraft(stub.id)

    if (
      tile &&
      !tile.error &&
      (tile.workspaceMode ?? 'sessions') === 'sessions' &&
      !sessions.some(session => session.id === stub.id) &&
      (focused === stub.id || titles[stub.id] || draft.attachments.length)
    ) {
      rows.set(stub.id, {
        cwd: stub.cwd ?? null,
        git_repo_root: stub.git_repo_root,
        id: stub.id,
        main: false,
        selected: focused === stub.id
      })
    }
  }

  if (archived) {
    return null
  }

  return [...rows.values()].map(row => {
    // These are placement facts, not invented backend rows. Use the same
    // module as optimistic sessions so detached drafts belong to Home.
    const placement = { cwd: row.cwd, git_repo_root: row.git_repo_root, id: row.id } as SessionInfo

    if (
      (projectId && sessionBucketId(placement, projects) !== projectId) ||
      !sessionMatchesProjectFilter(placement, filter, projects)
    ) {
      return null
    }

    return (
      <button
        aria-current={row.selected ? 'page' : undefined}
        className={cn(
          'mx-2 flex shrink-0 items-center rounded-md px-2 py-2 text-left text-sm hover:bg-sidebar-accent',
          row.selected && 'bg-sidebar-accent text-sidebar-accent-foreground'
        )}
        data-sidebar-draft-id={row.id}
        data-sidebar-draft-project={sessionBucketId(placement, projects) ?? undefined}
        key={row.id}
        onClick={() => {
          if (row.main) {
            revealTreePane('workspace')
            noteActiveTreeGroup(null)
          } else {
            focusOpenSession(row.id)
          }
        }}
        type="button"
      >
        <span className="truncate">{titles[row.id] || t.sidebar.nav['new-session']}</span>
      </button>
    )
  })
}
