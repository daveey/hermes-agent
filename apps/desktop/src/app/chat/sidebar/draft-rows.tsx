import { useStore } from '@nanostores/react'
import { useMemo } from 'react'
import { useLocation } from 'react-router'

import { NEW_CHAT_ROUTE } from '@/app/routes'
import { useMainSidebarDisplayCwd } from '@/app/session/new-session-cwd'
import { noteActiveTreeGroup, revealTreePane } from '@/components/pane-shell/tree/store'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $draftTitles, $freshDraftKey, takeSessionDraft } from '@/store/composer'
import { readDraftRetry } from '@/store/composer-draft-retry'
import { $projectOwnerBySessionId, $projects, $projectTree } from '@/store/projects'
import {
  $activeSessionId,
  $freshDraftReady,
  $selectedStoredSessionId,
  $sessions,
  $unlistedSessionOwnerRows
} from '@/store/session'
import { $focusedSessionIsTile, $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionTiles, focusOpenSession } from '@/store/session-states'

import { knownProjectsOf, sessionBucketId, sessionMatchesProjectFilter } from './projects/workspace-groups'

interface DraftRow {
  id: string
  cwd: null | string
  git_repo_root?: string | null
  selected: boolean
  main: boolean
  /** A first message the hub never accepted: parked as a retry draft in this chat. */
  unsent?: boolean
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
  const cwd = useMainSidebarDisplayCwd()
  const key = useStore($freshDraftKey)
  const titles = useStore($draftTitles)
  const sessions = useStore($sessions)
  const stubs = useStore($unlistedSessionOwnerRows)
  const tiles = useStore($sessionTiles)
  const projects = useStore($projects)
  const projectTree = useStore($projectTree)
  const projectOwners = useStore($projectOwnerBySessionId)
  const knownProjects = useMemo(() => knownProjectsOf(projectTree), [projectTree])
  const rows = new Map<string, DraftRow>()

  if (ready && !runtime && !selected && pathname === NEW_CHAT_ROUTE) {
    rows.set(key, { cwd: cwd || null, id: key, main: true, selected: !tileFocused })
  }

  for (const stub of stubs) {
    const tile = tiles.find(candidate => candidate.storedSessionId === stub.id)
    const draft = takeSessionDraft(stub.id)

    // A first send the hub refused parks its words as a retry draft in a chat the server never
    // listed: the row stays (tile or not) until the send lands, so the words are never orphaned.
    const unsent = Boolean(readDraftRetry(stub.id))

    if (
      !sessions.some(session => session.id === stub.id) &&
      (unsent ||
        (tile &&
          !tile.error &&
          (tile.workspaceMode ?? 'sessions') === 'sessions' &&
          (focused === stub.id || titles[stub.id] || draft.attachments.length)))
    ) {
      rows.set(stub.id, {
        cwd: stub.cwd ?? null,
        git_repo_root: stub.git_repo_root,
        id: stub.id,
        main: false,
        selected: focused === stub.id,
        unsent
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
      (projectId && sessionBucketId(placement, projects, projectOwners, knownProjects) !== projectId) ||
      !sessionMatchesProjectFilter(placement, filter, projects, projectOwners, knownProjects)
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
        data-sidebar-draft-project={sessionBucketId(placement, projects, projectOwners, knownProjects) ?? undefined}
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
        <span className="truncate">
          {row.unsent ? `${t.sidebar.nav['unsent-retry'] ?? 'Not sent \u2013 retry:'} ` : ''}
          {titles[row.id] || t.sidebar.nav['new-session']}
        </span>
      </button>
    )
  })
}
