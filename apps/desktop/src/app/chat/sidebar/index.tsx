import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router'

import { PlatformAvatar } from '@/app/messaging/platform-icon'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from '@/components/ui/context-menu'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { KbdGroup } from '@/components/ui/kbd'
import { SearchField } from '@/components/ui/search-field'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem
} from '@/components/ui/sidebar'
import { Tip, TipKeybindLabel } from '@/components/ui/tooltip'
import { type SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { comboTokens } from '@/lib/keybinds/combo'
import { sessionCreatedAt } from '@/lib/session-branch-tree'
import { normalizeSessionSource, sessionSourceLabel } from '@/lib/session-source'
import { useExecutionObservationClock } from '@/lib/use-execution-observation-clock'
import { cn } from '@/lib/utils'
import { markSidebarFirstRows } from '@/store/boot-snapshot'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $activeConnectionId } from '@/store/connections'
import { $cronJobs } from '@/store/cron'
import { recordAction } from '@/store/desktop-metrics'
import { $hiddenGatewayIds } from '@/store/gateway-visibility'
import { $showsAdvancedChrome } from '@/store/interface-mode'
import { $bindings } from '@/store/keybinds'
import {
  $dismissedAutoProjectIds,
  $panesFlipped,
  $pinnedSessionIds,
  $sidebarCardRows,
  $sidebarCronOpen,
  $sidebarFiltersActive,
  $sidebarGrouping,
  $sidebarMessagingOpenIds,
  $sidebarOrdering,
  $sidebarPinsOpen,
  $sidebarPrDataWanted,
  $sidebarPrFilter,
  $sidebarProfileFilter,
  $sidebarProjectFilter,
  $sidebarProjectOrderIds,
  $sidebarRecentsOpen,
  $sidebarSessionOrderIds,
  $sidebarSessionOrderManual,
  $sidebarShowAllSessions,
  $sidebarShowArchived,
  $sidebarShowAutomated,
  $sidebarStatusFilter,
  $sidebarWorkspaceOrderIds,
  $sidebarWorkspaceParentOrderIds,
  filterVisibleProjects,
  pinSession,
  SESSION_SEARCH_FOCUS_EVENT,
  setPinnedSessionOrder,
  setSidebarCronOpen,
  setSidebarPinsOpen,
  setSidebarProjectOrderIds,
  setSidebarRecentsOpen,
  setSidebarSessionOrderIds,
  setSidebarSessionOrderManual,
  setSidebarWorkspaceOrderIds,
  setSidebarWorkspaceParentOrderIds,
  SIDEBAR_SESSIONS_PAGE_SIZE,
  toggleSidebarMessagingOpen,
  unpinSession
} from '@/store/layout'
import { notifyError } from '@/store/notifications'
import {
  $newChatProfile,
  $profileColors,
  $profiles,
  $profileScope,
  type AgentProfileRoute,
  ALL_PROFILES,
  messagingTotalsKey,
  sidebarProfileForScope
} from '@/store/profile'
import { $profileRailVisible } from '@/store/profile-rail-prefs'
import { $projectScope, ALL_PROJECTS, exitProjectScope } from '@/store/project-scope'
import {
  $activeProjectId,
  $newProjectDropPlacement,
  $projectOwnerBySessionId,
  $projects,
  $projectTree,
  $projectTreeLoading,
  $reposScanning,
  enterProject,
  followEnteredProjectCwd,
  openProjectCreate,
  refreshProjects,
  refreshProjectTree,
  scanAndRecordRepos
} from '@/store/projects'
import {
  $prBranchBySession,
  $pullRequestsByBranch,
  recoverSessionPullRequests,
  refreshPullRequests,
  sessionPrKey
} from '@/store/pull-requests'
import { openRouteTile } from '@/store/route-tiles'
import {
  $cronSessions,
  $gatewayState,
  $messagingPlatformTotals,
  $messagingSessions,
  $messagingTruncated,
  $sessionProfilesTruncated,
  $sessions,
  $sessionsLoadError,
  $sessionsLoading,
  $unreadFinishedSessionIds,
  markAllSessionsRead,
  sessionPinId
} from '@/store/session'
import { $sessionDotStateById } from '@/store/session-dot-state'
import { $focusedSessionIsTile, $focusedStoredSessionId } from '@/store/session-focus'
import { $unconfirmedPinWrites } from '@/store/session-pin-sync'
import { $removedSessionIds } from '@/store/session-removal'
import { ackAllSessionsRead } from '@/store/session-unread'
import { markSessionUnread } from '@/store/session-unread-remote'
import { sessionHiddenAsAutomated } from '@/store/sidebar-automated'
import { $sidebarSessionRank, $sidebarSessionRankIds } from '@/store/sidebar-sort'

import { type AppView } from '../../routes'
import type { SidebarNavItem } from '../../types'
import { type NewSessionSplitHandler, startNewSessionDrag } from '../new-session-drag'

import { useArchivedView } from './archived-view'
import { SidebarPinnedAndBackgroundSections } from './background-section'
import { SidebarSectionAddButton } from './chrome'
import { SidebarCronJobsSection } from './cron-jobs-section'
import { SidebarDraftSections } from './draft-sections'
import {
  enteredContentExists,
  enteredPresentation,
  EnteredPresentationMenu,
  enteredPrimaryChrome,
  EnteredProjectMembers,
  useEnteredMember
} from './entered-project-members'
import { SidebarFilterMenu } from './filter-menu'
import { buildGatewaySessionGroups, scopeGatewaySessionGroups, useGatewaySessionGroups } from './gateway-group-model'
import { SidebarLoadMoreRow } from './load-more-row'
import { useSidebarNavItems } from './nav-items'
import { orderByIds, reconcileOrderIds, resolveManualSessionOrderIds, sameIds } from './order'
import { filterSessionsByProfileScope } from './profile-scope'
import { ProfileRail } from './profile-switcher'
import { ProjectDialog } from './project-dialog'
import { sidebarProjectPolicy as projectPolicy } from './project-filter'
import {
  excludeProjectSessions,
  knownProjectsOf,
  orderProjectsByIds,
  ProjectBackRow,
  projectTreeCwd,
  sessionBucketId,
  sessionRecency as sessionTime,
  type SidebarProjectTree,
  type SidebarWorkspaceTree,
  sortProjectsForOverview,
  StartWorkButton,
  useGatewayProjectTree,
  withProvisionalHome
} from './projects'
import { presentationNavigation as navigation } from './projects/presentation-navigation'
import { useSessionOwners } from './projects/use-gateway-project-tree'
import { WorktreeDialog } from './projects/worktree-dialog'
import { mergeSearchResults } from './search-results'
import { SidebarSearchEmpty, SidebarSearchFailure } from './search-states'
import {
  hasSessionArea,
  SidebarBlankState,
  SidebarLoadErrorState,
  SidebarPinnedEmptyState,
  SidebarSessionSkeletons,
  SidebarStorageCorruptNotice
} from './section-states'
import { GlobalSessionRenameDialog } from './session-actions-menu'
import { buildSessionByAnyId, resolvePinnedSessions, useOfflineFallback } from './session-index'
import { SidebarSessionsSection, VIRTUALIZE_THRESHOLD } from './sessions-section'
import { CONTEXT_SPLIT_KIT, SplitSubmenu } from './split-submenu'
import { useOverviewPreviews } from './use-overview-previews'
import { useServerSessionSearch } from './use-server-session-search'
import { useSessionFilters } from './use-session-filters'
import { useSidebarDndSensors } from './use-sidebar-dnd-sensors'
import { useSidebarNavCollapsed } from './use-sidebar-nav-collapsed'

export { mergeSearchResults, searchResultToSession, stripFtsMarkers } from './search-results'

// Non-session groups (messaging platforms) stay compact: show a few rows up
// front, reveal more in larger steps on demand. Keeps a busy platform from
// dominating the sidebar before the user asks to see it.
const NON_SESSION_INITIAL_ROWS = 3
const NON_SESSION_LOAD_STEP = 10

// How long after connecting to warm the project tree for someone who isn't in
// the grouped view. Long enough that the flat list — the thing actually on
// screen — has the connection to itself first.
const PROJECT_TREE_WARM_MS = 2_000

// Two modes via the `compact` height variant (styles.css):
//   tall    → each section is shrink-0, capped, its own scroller; Sessions is flex-1.
//   compact → COMPACT_FLAT drops the caps so the whole stack scrolls as one.
// Sections stay shrink-0 so none can be squeezed below its content and bleed onto
// the next — the flexbox `min-height: auto` overlap trap that caused the bug.
const COMPACT_FLAT = 'compact:max-h-none compact:overflow-visible'

// Vertical scroll only — never a horizontal bar from glow bleed, long titles,
// etc. The bar itself only shows while the pointer is in the list.
const SCROLL_Y = 'overflow-y-auto overflow-x-hidden overscroll-contain scrollbar-fade'

// The outer list reserves its bar's width whether or not one is showing, so
// filtering or collapsing a section doesn't reflow every row sideways. Only the
// outer one: nested scrollers would each reserve their own and stack the inset.
const SCROLL_GUTTER = '[scrollbar-gutter:stable]'

// A non-session group's scroll body: own scroller when tall, flattened when compact.
const GROUP_BODY = cn(SCROLL_Y, COMPACT_FLAT)

// Section-header action icons stay hidden until the whole header row is hovered
// (group/section lives on SidebarSectionHeader), mirroring the artifacts/file
// browser header affordances. focus-visible keeps them keyboard-reachable.
const HEADER_ACTION_BTN =
  'text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/section:opacity-100 focus-visible:opacity-100'

// The view toggle (overview group toggle / in-project back) is the one control
// that stays visible at all times — it's the stable navigation affordance, not
// a hover-revealed action.
const HEADER_NAV_BTN =
  'text-(--ui-text-tertiary) opacity-70 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground hover:opacity-100 focus-visible:opacity-100'

interface ChatSidebarProps extends React.ComponentProps<typeof Sidebar> {
  currentView: AppView
  onNavigate: (item: SidebarNavItem) => void
  onLoadMoreSessions: () => Promise<void> | void
  onRetrySessions: () => Promise<void> | void
  onLoadMoreMessaging?: (platform: string) => Promise<void> | void
  onResumeSession: (sessionId: string, session?: SessionInfo) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string, session?: SessionInfo) => void
  onBranchSession: (sessionId: string) => void
  onNewSessionInWorkspace: (path: null | string, owner?: AgentProfileRoute) => void
  /** Create a brand-new session and open it as a tile. `dir` is the dock edge
   *  (or `center` to stack a tab); `anchor`/`before` optionally pin it to a
   *  specific zone / tab-strip slot, and `cwd` pins it to a project's path —
   *  used by the new-session drags (the "New session" row and the project "+"
   *  buttons), which land a fresh session exactly where it's dropped. The
   *  context-menu "Open in split" path passes just a `dir`. */
  onNewSessionSplit: NewSessionSplitHandler
  onManageCronJob: (jobId: string) => void
  onTriggerCronJob: (jobId: string) => Promise<void>
}

export function ChatSidebar({
  currentView: routeView,
  onNavigate,
  onLoadMoreSessions,
  onRetrySessions,
  onLoadMoreMessaging,
  onResumeSession: resumeSession,
  onDeleteSession,
  onArchiveSession,
  onBranchSession,
  onNewSessionInWorkspace,
  onNewSessionSplit,
  onManageCronJob,
  onTriggerCronJob
}: ChatSidebarProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const { pathname } = useLocation()
  const navItems = useSidebarNavItems()
  const showsAdvancedChrome = useStore($showsAdvancedChrome)

  const panesFlipped = useStore($panesFlipped)
  const grouping = useStore($sidebarGrouping)
  const ordering = useStore($sidebarOrdering)
  const statusFilter = useStore($sidebarStatusFilter)
  const persistedProjectFilter = useStore($sidebarProjectFilter)
  const profileFilter = useStore($sidebarProfileFilter)
  const prFilter = useStore($sidebarPrFilter)
  const prDataWanted = useStore($sidebarPrDataWanted)
  const prBranchOverrides = useStore($prBranchBySession)
  const pullRequests = useStore($pullRequestsByBranch)
  const filtersActive = useStore($sidebarFiltersActive)
  const showArchived = useStore($sidebarShowArchived)
  const showAutomated = useStore($sidebarShowAutomated)
  const cardRows = useStore($sidebarCardRows)
  const archive = useArchivedView(showArchived)
  const archivedSessions = archive.sessions
  const dotStates = useStore($sessionDotStateById)
  // The active sort key as an id order. The flat list applies it within its
  // dividers; groups apply it to their own lanes.
  const sortOrderIds = useStore($sidebarSessionRankIds)
  const sessionRank = useStore($sidebarSessionRank)
  const agentsGrouped = grouping === 'project'
  const showAllSessions = useStore($sidebarShowAllSessions)
  const pinnedSessionIds = useStore($pinnedSessionIds)
  const unconfirmedPinWrites = useStore($unconfirmedPinWrites)
  const pinsOpen = useStore($sidebarPinsOpen)
  const agentsOpen = useStore($sidebarRecentsOpen)
  const cronOpen = useStore($sidebarCronOpen)
  // The sidebar highlight tracks the FOCUSED session — the interacted tile's
  // tab, else the main selection — so it stays 1:1 with whatever tab is active.
  const selectedSessionId = useStore($focusedStoredSessionId)
  const focusedSessionIsTile = useStore($focusedSessionIsTile)
  const currentView = focusedSessionIsTile ? 'chat' : routeView
  const { onResumeSession, sessions } = useOfflineFallback(useStore($sessions), resumeSession)
  const cronSessions = useStore($cronSessions)
  const cronJobs = useStore($cronJobs)
  const messagingSessions = useStore($messagingSessions)
  const messagingPlatformTotals = useStore($messagingPlatformTotals)
  const messagingTruncated = useStore($messagingTruncated)
  const sessionsLoading = useStore($sessionsLoading)
  const sessionsLoadError = useStore($sessionsLoadError)
  const sessionProfilesTruncated = useStore($sessionProfilesTruncated)
  const unreadCount = useStore($unreadFinishedSessionIds).length
  const profiles = useStore($profiles)
  const profileScope = useStore($profileScope)
  const activeConnectionId = useStore($activeConnectionId)

  // Toggle the persisted read-state watermark from a row menu. The row's own
  // `unread` prop mirrors what the dot paints; flip it and let the backend
  // become the truth (optimistic update + rollback in markSessionUnread).
  const toggleUnread = (storedId: string) => {
    const row = $sessions.get().find(r => r.id === storedId)

    if (!row) {
      return
    }

    markSessionUnread(storedId, row.unread !== true).catch(err => notifyError(err, s.row.unreadFailed))
  }

  // Only surface the profile switcher when more than one profile exists, so
  // single-profile users see the unchanged sidebar.
  const multiProfile = profiles.length > 1
  // Gate ALL-profiles grouping on multiProfile too: if a user drops back to one
  // profile while scope is still ALL (persisted), the rail is hidden and they'd
  // otherwise be stuck in the grouped view with no way out.
  const showAllProfiles = multiProfile && profileScope === ALL_PROFILES
  const messagingProfile = sidebarProfileForScope(profileScope)
  const agentOrderIds = useStore($sidebarSessionOrderIds)
  const agentOrderManual = useStore($sidebarSessionOrderManual)
  const workspaceOrderIds = useStore($sidebarWorkspaceOrderIds)
  const workspaceParentOrderIds = useStore($sidebarWorkspaceParentOrderIds)
  const projectOrderIds = useStore($sidebarProjectOrderIds)
  const projects = useStore($projects)
  const projectTree = useStore($projectTree)
  const projectOwners = useStore($projectOwnerBySessionId)
  const knownProjects = useMemo(() => knownProjectsOf(projectTree), [projectTree])

  const projectTreeLoading = useStore($projectTreeLoading)
  const removedSessionIds = useStore($removedSessionIds)
  const reposScanning = useStore($reposScanning)
  const activeProjectId = useStore($activeProjectId)
  const projectScope = useStore($projectScope)
  const gatewayState = useStore($gatewayState)
  const dismissedAutoProjects = useStore($dismissedAutoProjectIds)
  const newSessionCombo = useStore($bindings)['session.new']?.[0]
  const newSessionKbd = newSessionCombo ? comboTokens(newSessionCombo) : []
  const [searchQuery, setSearchQuery] = useState('')
  const [newSessionKbdFlash, setNewSessionKbdFlash] = useState(false)

  const { navCollapsed, toggleNavCollapsed } = useSidebarNavCollapsed()

  const [messagingLoadMorePending, setMessagingLoadMorePending] = useState<Record<string, boolean>>({})
  const [recentsLoadMorePending, setRecentsLoadMorePending] = useState(false)
  const messagingOpenIds = useStore($sidebarMessagingOpenIds)
  // Per-platform count of rows currently revealed (starts at NON_SESSION_INITIAL_ROWS).
  const [messagingVisible, setMessagingVisible] = useState<Record<string, number>>({})
  const searchInputRef = useRef<HTMLInputElement>(null)
  const trimmedQuery = searchQuery.trim()

  // Hotkey (session.focusSearch) → focus the field once it's mounted.
  useEffect(() => {
    const onFocus = () => searchInputRef.current?.focus({ preventScroll: true })

    window.addEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)

    return () => window.removeEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)
  }, [])

  // Flash the ⌘N hint full-opacity (no transition) for the press, so hitting
  // the shortcut visibly pings its affordance in the sidebar.
  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout> | undefined

    const onShortcut = () => {
      setNewSessionKbdFlash(true)
      clearTimeout(timeout)
      timeout = setTimeout(() => setNewSessionKbdFlash(false), 140)
    }

    window.addEventListener('hermes:new-session-shortcut', onShortcut)

    return () => {
      window.removeEventListener('hermes:new-session-shortcut', onShortcut)
      clearTimeout(timeout)
    }
  }, [])

  const activeSidebarSessionId = currentView === 'chat' ? selectedSessionId : null
  const profileRailVisible = useStore($profileRailVisible)

  const dndSensors = useSidebarDndSensors()

  // Profile scope = the "workspace switcher" context. Concrete scope shows only
  // that profile's sessions (clean rows, no per-row tags); ALL fans every
  // profile in. Grouped rendering stays gated on `showAllProfiles` (multi-profile
  // + ALL) so a single-profile user is never stranded in a grouped view with no
  // rail — but the *data* still has to fan in when the persisted scope is ALL
  // (Grouping → Profile). Filtering that pool against the `__all__` sentinel
  // matches nothing and empties recents + pins.
  // Archived rows are excluded from the sessions query, so Archived is a view of
  // its own set rather than a filter over this one — a flat list of archived
  // rows, no project tree, no date or status dividers.
  const scopedSessions = useMemo(() => {
    const pool = showArchived ? archivedSessions : sessions

    return filterSessionsByProfileScope(pool, profileScope)
  }, [sessions, archivedSessions, showArchived, profileScope])

  // The tree the sidebar renders and filters by: the backend's, plus a Home
  // node when a fresh row only Home can show arrives before the tree has one.
  // Built from the unfiltered rows, so filters narrow it like any other node.
  const visibleProjectTree = useMemo(
    () =>
      showArchived
        ? projectTree
        : withProvisionalHome(projectTree, scopedSessions, projects, {
            known: knownProjects,
            owners: projectOwners,
            removed: removedSessionIds
          }),
    [showArchived, projectTree, scopedSessions, projects, knownProjects, projectOwners, removedSessionIds]
  )

  // The persisted project filter's storage is shared across profiles, so ids
  // picked in another profile don't resolve in the active one and the raw
  // membership whitelist empties every tier of the sidebar (#96246). Narrow
  // to ids the ACTIVE tree resolves; dead ids are inert, not fatal.
  const navigationOwners = useSessionOwners()

  const navigationGroups = useMemo(
    () =>
      navigation.catalog(
        visibleProjectTree,
        scopedSessions,
        navigationOwners.foreignOwner,
        activeConnectionId ?? undefined
      ),
    [visibleProjectTree, scopedSessions, navigationOwners, activeConnectionId]
  )

  const projectFilter = useMemo(
    () => navigation.expandFilter(persistedProjectFilter, navigationGroups),
    [persistedProjectFilter, navigationGroups]
  )

  // One predicate for the status/project filters, so the flat list and the
  // project lanes narrow by the same rule. A project lane holds rows the loaded
  // page may not, so it has to be answerable per session rather than by
  // membership in the filtered set. Detached rows file under the Home bucket id
  // (same rule as the overview overlay), so filtering to Home keeps Home's rows.
  const hiddenGatewayIds = useStore($hiddenGatewayIds)
  const gatewayRegistry = useStore($connectionsRegistry)

  const executionRows = useMemo(
    () => [sessions, messagingSessions, projectTree],
    [sessions, messagingSessions, projectTree]
  )

  const executionNow = useExecutionObservationClock(executionRows)

  const { filtersNarrow, pluginFilters, sessionMatchesFilters, visibleSessions } = useSessionFilters({
    dotStates,
    executionNow,
    gatewayRegistry,
    hiddenGatewayIds,
    knownProjects,
    navigationGroups,
    pinnedSessionIds,
    prFilter,
    profileFilter,
    projectFilter,
    projectOwners,
    projects,
    pullRequests,
    scopedSessions,
    showAllProfiles,
    showAutomated,
    statusFilter
  })

  // Recents by activity (last_active || started_at). User send stamps
  // last_active immediately. Ordering by status doesn't sort here — it re-slots
  // rows *inside* whatever dividers are on, via sortOrderIds below — so the
  // date buckets stay chronological either way.
  // [daveey] Except under the "created" key, which must not move a row when
  // it gets a reply — see sessions-section's `timeOf`.
  const sortedSessions = useMemo(() => {
    const timeOf = ordering === 'created' ? sessionCreatedAt : sessionTime

    return [...visibleSessions].sort((a, b) => timeOf(b) - timeOf(a))
  }, [visibleSessions, ordering])

  const visibleCronSessions = useMemo(
    () => filterSessionsByProfileScope(cronSessions, profileScope),
    [cronSessions, profileScope]
  )

  // [daveey] A bot profile's platform threads (Slack followers…) are
  // automated too: same predicate as recents and the project lanes.
  const visibleMessagingSessions = useMemo(
    () =>
      filterSessionsByProfileScope(messagingSessions, profileScope).filter(
        session =>
          !sessionHiddenAsAutomated(session, showAutomated, {
            nowSeconds: executionNow,
            dotState: dotStates[session.id] ?? 'idle',
            pinnedIds: pinnedSessionIds
          })
      ),
    [messagingSessions, profileScope, showAutomated, dotStates, pinnedSessionIds, executionNow]
  )

  // Index sessions by every id a pin might be stored under — recents, cron,
  // AND messaging, since all three can be pinned (see session-index.ts).
  const sessionByAnyId = useMemo(
    () => buildSessionByAnyId(visibleSessions, visibleCronSessions, visibleMessagingSessions),
    [visibleSessions, visibleCronSessions, visibleMessagingSessions]
  )

  // Local pin ids first (hand-picked order), then server-flagged pins the
  // local set doesn't know about — a backend `pinned=1` row must never be
  // invisible just because localStorage is cold or was clobbered (#85969) —
  // minus the rows whose flag our own in-flight pin write already contradicts.
  const pinnedSessions = useMemo(
    () =>
      resolvePinnedSessions(
        pinnedSessionIds,
        sessionByAnyId,
        [...visibleSessions, ...cronSessions, ...messagingSessions],
        unconfirmedPinWrites
      ),
    [pinnedSessionIds, sessionByAnyId, visibleSessions, cronSessions, messagingSessions, unconfirmedPinWrites]
  )

  // Every id a pin is reachable under: the raw stored ids, plus BOTH identities
  // of each session we resolved one to. A pin is stored on the durable lineage
  // root, but the lists that must filter it out are fed from three independent
  // fetches (recents, the messaging slice, the backend project tree) and each
  // can surface the same conversation under either its live tip or its root.
  // Comparing one identity against the other is how a pinned session ended up
  // rendered twice — once in Pinned, once in its project group.
  const pinnedIdentitySet = useMemo(() => {
    const ids = new Set(pinnedSessionIds)

    for (const session of pinnedSessions) {
      ids.add(session.id)

      if (session._lineage_root_id) {
        ids.add(session._lineage_root_id)
      }
    }

    return ids
  }, [pinnedSessionIds, pinnedSessions])

  // A pinned session belongs to the Pinned section and nowhere else, so every
  // other list filters it out. Match on either identity the row carries — a
  // backend snapshot can surface either side of a compression tip rotation.
  const isPinnedSession = useCallback(
    (session: SessionInfo) =>
      pinnedIdentitySet.has(session.id) ||
      (session._lineage_root_id != null && pinnedIdentitySet.has(session._lineage_root_id)),
    [pinnedIdentitySet]
  )

  // What the project tree drops: pins (they live in their own section) plus
  // anything the active filters exclude, so filtering works the same whether
  // you're looking at the flat list or the lanes.
  const isHiddenFromProjects = useCallback(
    (session: SessionInfo) => isPinnedSession(session) || (filtersNarrow && !sessionMatchesFilters(session)),
    [isPinnedSession, filtersNarrow, sessionMatchesFilters]
  )

  const { searchPending, serverMatches, searchFailed, retrySearch } = useServerSessionSearch(trimmedQuery, profileScope)

  const searchResults = useMemo(
    () => mergeSearchResults(sortedSessions, trimmedQuery, serverMatches, sessionByAnyId, searchPending),
    [sortedSessions, trimmedQuery, serverMatches, sessionByAnyId, searchPending]
  )

  const unpinnedAgentSessions = useMemo(
    () => sortedSessions.filter(s => !isPinnedSession(s)),
    [sortedSessions, isPinnedSession]
  )

  useEffect(() => {
    const next = resolveManualSessionOrderIds(
      unpinnedAgentSessions.map(s => s.id),
      agentOrderIds,
      agentOrderManual
    )

    if (!next.length && agentOrderManual) {
      setSidebarSessionOrderManual(false)
    }

    if (!next.length && agentOrderIds.length) {
      setSidebarSessionOrderIds([])

      return
    }

    if (next.length && !sameIds(next, agentOrderIds)) {
      setSidebarSessionOrderIds(next)
    }
  }, [agentOrderIds, agentOrderManual, unpinnedAgentSessions])

  // Recents render in recency order. The hand-picked order is layered on per date group inside the
  // section (orderRowsWithinGroups) rather than baked into the list here, so a drag ranks a row
  // among its own day's chats instead of flattening the whole sidebar into an undated manual mode.
  const agentSessions = unpinnedAgentSessions
  const { owners, ownerKey, tree: gatewayProjectTree } = useGatewayProjectTree(visibleProjectTree, agentSessions)

  // Recents are local-only: messaging-platform sessions are fetched as their
  // own slice ($messagingSessions) and rendered in self-managed per-platform
  // sections below, so there is no source-grouping magic to untangle here.
  //
  // Workspace grouping is a `project -> repo -> lane -> sessions` tree computed
  // authoritatively on the backend (projects.tree). Parents reorder via
  // workspaceParentOrderIds; worktrees within a parent via workspaceOrderIds.
  const worktreeGroupingActive = agentsGrouped && !showArchived
  const gatewayReady = gatewayState === 'open'

  // The backend project tree is a structural snapshot, NOT a per-message feed.
  // Refresh it on structural edges only — entering the grouped view, a profile
  // switch, gateway (re)connect — plus the once-per-run disk scan. Live session
  // changes between refreshes are reflected by the in-memory overlay
  // (overlayLiveLanes / overlayLivePreviews) off `$sessions`, so a turn
  // completing does NOT re-run the heavy list_sessions_rich scan. Project
  // mutations refresh the tree from their own store actions.
  useEffect(() => {
    if (!gatewayReady) {
      return
    }

    if (worktreeGroupingActive) {
      void refreshProjects()

      // The all-profiles tree is served off every profile's databases at once
      // and deliberately leaves discovery out — a repo with no sessions is the
      // same repo in every profile, so scanning here would multiply empty lanes
      // by the profile count and write the result into profiles the user isn't
      // driving.
      if (showAllProfiles) {
        void refreshProjectTree()

        return
      }

      // Paint the list from the fast tree fetch (explicit projects + repos from
      // existing sessions / the backend cache) FIRST, then kick off the heavy
      // home-dir git crawl so newly-discovered repos fold in afterward — instead
      // of the crawl blocking the first render.
      void refreshProjectTree().finally(() => void scanAndRecordRepos())

      return
    }

    // Flat view: warm the tree in the background anyway. Fetching it only on
    // the switch meant the first switch of every run paid for the whole round
    // trip behind a skeleton, and the menu's Project filter had nothing to
    // list until you'd visited the grouped view at least once.
    const warm = window.setTimeout(() => void refreshProjectTree(), PROJECT_TREE_WARM_MS)

    return () => window.clearTimeout(warm)
  }, [activeConnectionId, worktreeGroupingActive, showAllProfiles, profileScope, gatewayReady])

  // Widen the existing tree query when the user expands previews, without
  // repeating repo discovery. Initial load/scope changes use the effect above.
  useEffect(
    () =>
      $sidebarShowAllSessions.listen(() => {
        if (gatewayReady && worktreeGroupingActive) {
          void refreshProjectTree()
        }
      }),
    [gatewayReady, worktreeGroupingActive]
  )

  // Sessions the branch join can't answer for get one look at their own
  // transcript — a `gh pr create` in there names the PR outright. Backfills
  // whatever is loaded, whether or not the badge is on: gating it on the badge
  // meant switching PR on showed a half-empty list until a second pass caught
  // up. One request per batch of never-scanned rows, and the scanned set makes
  // that batch empty from the second pass on, so this settles to nothing.
  useEffect(() => {
    if (!gatewayReady) {
      return
    }

    const warm = window.setTimeout(() => void recoverSessionPullRequests(scopedSessions), PROJECT_TREE_WARM_MS)

    return () => window.clearTimeout(warm)
  }, [gatewayReady, scopedSessions])

  // PR state is only fetched for someone who asked to see it — the badge or the
  // filter — and it asks about the branches on screen, so the answer can't be
  // crowded out by a busy repo's newer PRs.
  const prLookupsByRepo = useMemo(() => {
    if (!prDataWanted) {
      return {}
    }

    const byRepo: Record<string, string[]> = {}

    for (const session of scopedSessions) {
      // The row's own key, so a session bound to a branch (or a PR number) it
      // was stamped with asks about THAT, not the branch it started on.
      const [root, lookup] = sessionPrKey(session)?.split('\n') ?? []

      if (root && lookup && !byRepo[root]?.includes(lookup)) {
        byRepo[root] = [...(byRepo[root] ?? []), lookup]
      }
    }

    return byRepo
    // prBranchOverrides is what `sessionPrKey` reads through — a recovered PR
    // has to re-ask with the key it just learned.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prDataWanted, scopedSessions, prBranchOverrides])

  // A stable identity for "the same question as last time", so a re-render that
  // rebuilds the map doesn't re-ask GitHub.
  const prQueryKey = JSON.stringify(
    Object.entries(prLookupsByRepo)
      .map(([root, lookups]) => [root, [...lookups].sort()] as const)
      .sort(([a], [b]) => a.localeCompare(b))
  )

  useEffect(() => {
    if (prQueryKey === '[]') {
      return
    }

    const byRepo = Object.fromEntries(JSON.parse(prQueryKey) as [string, string[]][])

    void refreshPullRequests(byRepo)

    // A PR opens, merges or gets closed on github.com, not in here — so like
    // the project tree, re-pull when the window comes back. The store's own
    // staleness window keeps a flurry of focus events to one call per repo.
    const onActive = () => {
      if (document.visibilityState !== 'hidden') {
        void refreshPullRequests(byRepo)
      }
    }

    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)

    return () => {
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [prQueryKey])

  // Out-of-band repo changes (a `git init` / `rm -rf` in another terminal) emit
  // no git events, so — like every git GUI — re-pull on window focus / tab
  // visibility instead of stranding the tree until a hard reload. The tree
  // fetch is cheap and runs every focus (picks up explicit create/delete +
  // session regrouping); the heavy disk crawl that surfaces brand-new repos is
  // throttled. Agent-driven changes already refresh via $workspaceChangeTick.
  useEffect(() => {
    if (!worktreeGroupingActive || !gatewayReady) {
      return
    }

    let lastScanAt = 0
    const SCAN_THROTTLE_MS = 30_000

    const onActive = () => {
      if (document.visibilityState === 'hidden') {
        return
      }

      void refreshProjects()
      void refreshProjectTree()

      // Discovery stays off while browsing every profile, for the reason the
      // first fetch leaves it out.
      if (showAllProfiles) {
        return
      }

      const now = Date.now()

      if (now - lastScanAt >= SCAN_THROTTLE_MS) {
        lastScanAt = now
        void scanAndRecordRepos(true)
      }
    }

    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)

    return () => {
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [worktreeGroupingActive, showAllProfiles, gatewayReady])

  // Apply the persisted repo + worktree orders to a project's repo subtrees.
  const orderRepos = useCallback(
    (repos: SidebarWorkspaceTree[]): SidebarWorkspaceTree[] =>
      orderByIds(repos, parent => parent.id, workspaceParentOrderIds).map(parent => ({
        ...parent,
        groups: orderByIds(parent.groups, group => group.id, workspaceOrderIds)
      })),
    [workspaceParentOrderIds, workspaceOrderIds]
  )

  // ── Projects: the single top-level model (authoritative, from the backend) ──
  // `projects.tree` unifies explicit projects + auto repos (worktrees folded); the desktop only layers view state
  // (dismissals, repo/lane order, overview sort). Membership is the backend tree's, never re-derived.
  const projectModel = useMemo<SidebarProjectTree[]>(() => {
    const sorted = sortProjectsForOverview(
      navigation
        .shown(
          filterVisibleProjects(gatewayProjectTree, dismissedAutoProjects),
          projectScope,
          navigationGroups,
          dismissedAutoProjects
        )
        // A filtered-out project drops its whole lane, header included — hiding
        // only its rows would leave a row of empty folders behind.
        .filter(project => !projectFilter.length || projectFilter.includes(project.id))
        .map(project =>
          excludeProjectSessions(
            {
              ...project,
              // Home is synthetic, so its name is ours to translate — every
              // other label is a repo basename or a name the user typed.
              label: project.isNoProject ? s.projects.home : project.label,
              repos: orderRepos(project.repos)
            },
            isHiddenFromProjects
          )
        ),
      activeProjectId,
      projectPolicy.order(ordering)
    )

    // Manual drag-order over the sorted list; dedupe by id (Windows path casing across profiles).
    const seen = new Set<string>()

    const deduped = sorted.filter(project => {
      if (seen.has(project.id)) {
        return false
      }

      seen.add(project.id)

      return true
    })

    return orderProjectsByIds(deduped, projectOrderIds, projectPolicy.order(ordering))
  }, [
    gatewayProjectTree,
    navigationGroups,
    projectScope,
    dismissedAutoProjects,
    orderRepos,
    activeProjectId,
    ordering,
    projectFilter,
    projectOrderIds,
    isHiddenFromProjects,
    s
  ])

  // The overview only renders in grouped mode; the model stays live regardless
  // so scoping is consistent across views.
  const agentProjectTree = worktreeGroupingActive ? projectModel : undefined

  // ── Project switcher (drill-in) ────────────────────────────────────────────
  // Grouped, single-profile view is a project switcher: ALL_PROJECTS shows the
  // overview (a list you click into); a concrete scope means you've "entered" a
  // project, so the Sessions list shows ONLY that project's worktrees/sessions.
  const projectsActive = Boolean(agentProjectTree?.length)

  // The overview node for the entered project (structure + counts, empty lanes).
  const {
    overview: overviewEnteredProject,
    members: groupedEnteredMembers,
    grouped
  } = enteredPresentation(agentProjectTree, projectScope, activeConnectionId)

  const inProject = Boolean(overviewEnteredProject)
  const enteredProjectId = overviewEnteredProject?.id

  const enteredMemberOptions = {
    gatewayReady,
    projectTree,
    scope: `${activeConnectionId}:${profileScope}`,
    grouped,
    orderRepos,
    isHiddenFromProjects,
    agentSessions,
    owners,
    removedSessionIds,
    projectOwners,
    projects,
    knownProjects,
    ownerKey,
    showAllProfiles
  }

  const primaryMember = useEnteredMember(overviewEnteredProject, enteredMemberOptions)

  const {
    project: enteredProject,
    content: enteredProjectContent,
    liveSessions: enteredProjectOverlaySessions,
    repoWorktrees: scopedRepoWorktrees,
    failed: projectLoadFailed,
    loading: projectLoading,
    retry: retryProject
  } = enteredPrimaryChrome(primaryMember, grouped)

  const hasEnteredContent = enteredContentExists(enteredProjectContent, groupedEnteredMembers)

  const lastProjectCwdSyncRef = useRef<null | string>(null)

  const syncProjectCwd = useCallback((project: SidebarProjectTree) => {
    const target = projectTreeCwd(project)

    if (target) {
      followEnteredProjectCwd(target)
    }
  }, [])

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (!inProject || !enteredProject) {
      lastProjectCwdSyncRef.current = null

      return
    }

    if (lastProjectCwdSyncRef.current === enteredProject.id) {
      return
    }

    syncProjectCwd(enteredProject)
    lastProjectCwdSyncRef.current = enteredProject.id
  }, [inProject, enteredProject, syncProjectCwd])

  // A persisted scope can go stale (project archived/removed, or a profile
  // switch swapped the whole catalog). Once projects have loaded, drop back to
  // the overview if the scoped id is gone.
  useEffect(() => {
    if (projectScope !== ALL_PROJECTS && projectsActive && !enteredProject) {
      exitProjectScope()
    }
  }, [projectScope, projectsActive, enteredProject])

  // The project overview (drill-in list) vs. the entered project's content.
  const projectOverview = projectsActive && !inProject ? agentProjectTree : undefined

  const overviewPreviews = useOverviewPreviews(projectOverview, agentSessions, projects, showAllSessions, {
    ownerKey,
    removed: removedSessionIds,
    owners: projectOwners,
    known: knownProjects,
    rank: sessionRank
  })

  // A row's "Show all" hydrates raw backend lanes, which — like the drill-in —
  // must go through the same exclusion as the previews above (pins, filter
  // misses, optimistic removals), or a pinned chat renders twice and a
  // just-deleted one comes back. The per-project count of loaded sessions
  // that exclusion hides also corrects the backend's `sessionCount` in the
  // "Show all N" label (a pin is always loaded — it renders in Pinned).
  const overviewHidden = useMemo(() => {
    const isHidden = (session: SessionInfo) => isHiddenFromProjects(session) || removedSessionIds.has(session.id)
    const counts: Record<string, number> = {}

    for (const session of sessions) {
      const projectId = isHidden(session) ? sessionBucketId(session, projects, projectOwners, knownProjects) : null

      if (projectId) {
        counts[projectId] = (counts[projectId] ?? 0) + 1
      }
    }

    return { isHidden, counts }
  }, [sessions, projects, projectOwners, knownProjects, isHiddenFromProjects, removedSessionIds])

  const onEnterProject = useCallback(
    (id: string) => {
      const project = projectModel.find(node => node.id === id)

      if (project) {
        syncProjectCwd(project)
      }

      enterProject(id)
    },
    [projectModel, syncProjectCwd]
  )

  // The section header must name what the section is showing. Grouped mode
  // reads "Projects" only while there IS a project switcher to show: a real
  // project row, or a tree still resolving (the loading state keeps the label
  // stable instead of flapping to "Sessions" and back). The synthetic Home
  // bucket alone is just the flat session list wearing a project costume —
  // with no real projects the section lists plain chat sessions, so it keeps
  // the "Sessions" label (#62537).
  const hasProjectRows = projectModel.some(node => !node.isNoProject)

  const sessionsLabel =
    inProject && enteredProject
      ? enteredProject.label
      : worktreeGroupingActive && (hasProjectRows || projectTreeLoading)
        ? s.projects.sectionLabel
        : s.sessions

  // Mirror the section's skeleton gate (projectsLoading + nothing to show yet):
  // while the skeleton is up there's no point also spinning the header count.
  const projectsSkeletonVisible =
    worktreeGroupingActive && projectTreeLoading && !projectOverview?.length && !(inProject && hasEnteredContent)

  const runKeyedLoad = useCallback(
    (
      key: string,
      load: ((key: string) => Promise<void> | void) | undefined,
      setPending: React.Dispatch<React.SetStateAction<Record<string, boolean>>>
    ) => {
      if (!load) {
        return
      }

      setPending(prev => ({ ...prev, [key]: true }))

      void Promise.resolve(load(key))
        .catch(() => undefined)
        .finally(() => setPending(({ [key]: _done, ...rest }) => rest))
    },
    []
  )

  const loadMoreForMessaging = useCallback(
    (platform: string) => runKeyedLoad(platform, onLoadMoreMessaging, setMessagingLoadMorePending),
    [onLoadMoreMessaging, runKeyedLoad]
  )

  // Reveal another batch of a platform's rows; fetch from the backend too if we
  // run past what's loaded and more remain on disk.
  const revealMoreMessaging = (platform: string, loaded: number, hasMore: boolean) => {
    const next = (messagingVisible[platform] ?? NON_SESSION_INITIAL_ROWS) + NON_SESSION_LOAD_STEP

    setMessagingVisible(prev => ({ ...prev, [platform]: next }))

    if (next > loaded && hasMore) {
      loadMoreForMessaging(platform)
    }
  }

  // Each messaging platform is its own self-managed section: split the
  // separately-fetched messaging slice by source, newest platform first, rows
  // within a platform by recency. Per-platform totals (when a "load more" has
  // resolved them) drive the count + whether more remain on disk.
  const messagingGroups = useMemo<MessagingSection[]>(() => {
    if (!visibleMessagingSessions.length) {
      return []
    }

    const bySource = new Map<string, SessionInfo[]>()
    // Rows this platform owns that the Pinned section is showing instead. The
    // backend's per-platform total counts them, so discount it or "load more"
    // promises rows that will never appear.
    const pinnedBySource = new Map<string, number>()

    for (const session of visibleMessagingSessions) {
      const sourceId = normalizeSessionSource(session.source)

      if (!sourceId) {
        continue
      }

      if (isPinnedSession(session)) {
        pinnedBySource.set(sourceId, (pinnedBySource.get(sourceId) ?? 0) + 1)

        continue
      }

      const list = bySource.get(sourceId) ?? []
      list.push(session)
      bySource.set(sourceId, list)
    }

    return [...bySource.entries()]
      .map(([sourceId, list]) => {
        const ordered = [...list].sort((a, b) => sessionTime(b) - sessionTime(a))
        const known = messagingPlatformTotals[messagingTotalsKey(messagingProfile, sourceId)]
        const unpinnedKnown = known == null ? null : Math.max(0, known - (pinnedBySource.get(sourceId) ?? 0))
        const total = Math.max(ordered.length, unpinnedKnown ?? 0)

        return {
          // Known exact total → more exist iff total exceeds loaded; otherwise
          // the seed fetch was capped, so assume more until a per-platform load
          // resolves the count.
          hasMore: unpinnedKnown != null ? unpinnedKnown > ordered.length : messagingTruncated,
          label: sessionSourceLabel(sourceId) ?? sourceId,
          sessions: ordered,
          sourceId,
          total
        }
      })
      .sort((a, b) => sessionTime(b.sessions[0]) - sessionTime(a.sessions[0]))
  }, [visibleMessagingSessions, messagingPlatformTotals, messagingTruncated, isPinnedSession, messagingProfile])

  // Recents and every messaging platform resolve owner groups the same way
  // ([connectionId, profile]), so a platform's groups line up with recents.
  const ownerGrouped = profileScope === ALL_PROFILES && grouping === 'profile'

  // [daveey] Hiding automated chats is the fork's default view, not a narrowed one: a profile whose
  // chats are all automated (or none) still keeps its empty section.
  const profileListNarrow =
    [hiddenGatewayIds, statusFilter, projectFilter, prFilter, pluginFilters].some(list => list.length > 0) ||
    (showAllProfiles && profileFilter.length > 0)

  const profileGroups = useGatewaySessionGroups(agentSessions, ownerGrouped, profileListNarrow)
  const connectionsRegistry = useStore($connectionsRegistry)
  const profileColors = useStore($profileColors)

  // The flat Sessions list always shows ALL recent sessions; Projects is a
  // parallel grouped view, not a filter on this one — nothing is hidden here.
  const displayAgentSessions = agentSessions

  // Pagination is scope-aware. In "All profiles" mode it tracks the global
  // unified set; scoped to one profile it tracks that profile's own truncation
  // flag — otherwise a huge default profile keeps "Load more" stuck on while
  // you browse a small one. The backend reports whether its page was capped
  // rather than an exact count, so no COUNT(*) runs per refresh.
  const loadedSessionCount = showAllProfiles ? sessions.length : scopedSessions.length

  // The archived view pages its own cursor — paging the live sessions list
  // from under it would just fold un-archived rows back in.
  const hasMoreSessions = archive.pick(
    archive.hasMore,
    showAllProfiles
      ? Object.values(sessionProfilesTruncated).some(Boolean)
      : Boolean(sessionProfilesTruncated[profileScope])
  )

  const displayRecentsCountRef = useRef(0)
  const loadedRecentsCountRef = useRef(0)
  displayRecentsCountRef.current = displayAgentSessions.length
  loadedRecentsCountRef.current = loadedSessionCount

  const onLoadMoreRecents = useCallback(async () => {
    if (recentsLoadMorePending) {
      return
    }

    setRecentsLoadMorePending(true)

    try {
      const startVisible = displayRecentsCountRef.current
      const targetVisible = startVisible + SIDEBAR_SESSIONS_PAGE_SIZE
      let lastLoaded = loadedRecentsCountRef.current

      // Project-less recents can be sparse in the global recent stream (because
      // project-scoped sessions are filtered out in the UI). Keep paging until
      // we actually reveal a full page of visible rows, or the backend window
      // stops growing.
      for (let attempt = 0; attempt < 6; attempt += 1) {
        await Promise.resolve(onLoadMoreSessions())
        await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve()))

        const visibleNow = displayRecentsCountRef.current
        const loadedNow = loadedRecentsCountRef.current

        if (visibleNow >= targetVisible) {
          break
        }

        if (loadedNow <= lastLoaded) {
          break
        }

        lastLoaded = loadedNow
      }
    } finally {
      setRecentsLoadMorePending(false)
    }
  }, [onLoadMoreSessions, recentsLoadMorePending])

  // Ranking by size is a question about the whole list ("what did I burn money
  // on"), so it drops the calendar dividers and ranks globally — "Today" above
  // the priciest session you have ever had would be a lie. Time- and
  // state-based keys stay bucketed, where they read correctly per day.
  const rankedGlobally = ordering === 'cost' || ordering === 'tokens'

  const displayAgentGroups = profileGroups

  // The recents list owns its own (virtualized) scroll container only when it's a
  // long flat list. In that case it must keep its scroller even in short mode, so
  // we don't flatten it (flattening would defeat virtualization). Short flat lists
  // and grouped views (profile groups or the worktree tree) flatten into the
  // single outer scroll instead.
  // Whichever grouping is active, the flat set of repo subtrees on screen — the
  // single source for reconciling repo/worktree order, whether repos hang off
  // the bare tree or are nested under projects.
  const activeRepoTrees = useMemo<SidebarWorkspaceTree[]>(
    () => (agentProjectTree ? agentProjectTree.flatMap(project => project.repos) : []),
    [agentProjectTree]
  )

  // Mirror the section's own virtualization inputs (the props it receives),
  // not the raw tree cache: agentProjectTree persists after leaving Project
  // grouping, and keying on it here while the section keys on projectOverview
  // (which is nulled the moment grouping changes) left the two disagreeing —
  // wrapper classes built for a virtualized list around a non-virtual one.
  // Entered-project content is the third prop that suppresses virtualization.
  const recentsVirtualizes =
    !displayAgentGroups?.length &&
    !projectOverview?.length &&
    !(inProject && (enteredProjectContent || groupedEnteredMembers)) &&
    displayAgentSessions.length >= VIRTUALIZE_THRESHOLD

  // Keep the persisted parent + worktree orders reconciled with what's on screen:
  // freshly-seen repos/worktrees surface at the top, vanished ones drop out of
  // the saved order.
  useEffect(() => {
    if (!activeRepoTrees.length) {
      return
    }

    const nextParents = reconcileOrderIds(
      activeRepoTrees.map(parent => parent.id),
      workspaceParentOrderIds
    )

    if (!sameIds(nextParents, workspaceParentOrderIds)) {
      setSidebarWorkspaceParentOrderIds(nextParents)
    }

    const nextWorktrees = reconcileOrderIds(
      activeRepoTrees.flatMap(parent => parent.groups.map(group => group.id)),
      workspaceOrderIds
    )

    if (!sameIds(nextWorktrees, workspaceOrderIds)) {
      setSidebarWorkspaceOrderIds(nextWorktrees)
    }
  }, [activeRepoTrees, workspaceParentOrderIds, workspaceOrderIds])

  // Skeletons mean "still loading", so they key off the UNFILTERED set. Keyed
  // off the filtered one, a filter that matches nothing showed skeletons on
  // every background refresh instead of the empty state.
  const showSessionSkeletons = archive.pick(archive.firstLoad, sessionsLoading && scopedSessions.length === 0)

  // [daveey] Cold-start timing: when this launch's sidebar first drew rows, and whether they were live or the boot
  // snapshot (hub.db rows / last projects tree) painted before the backend was up.
  const drawnRows = sortedSessions.length + (projectOverview?.length ?? 0)

  useEffect(() => {
    if (drawnRows > 0) {
      markSidebarFirstRows(drawnRows, !sessionsLoading)
    }
  }, [drawnRows, sessionsLoading])

  const showSessionSections = hasSessionArea({
    cronJobs: showsAdvancedChrome ? cronJobs.length : 0,
    loading: showSessionSkeletons || Boolean(sessionsLoadError) || filtersActive,
    messaging: messagingGroups.length,
    profileGroups: profileGroups?.length ?? 0,
    projects: projectModel.length,
    sessions: sortedSessions.length
  })

  // The sidebar's session-area mode — exposed as data-attributes so custom
  // skins can target project mode (overview vs. entered), archived, or search
  // without relying on internal class names. `data-sessions-project` carries
  // the entered project's id for per-project targeting.
  const sessionsMode: 'archived' | 'flat' | 'project' | 'projects' | 'search' = trimmedQuery
    ? 'search'
    : showArchived
      ? 'archived'
      : inProject
        ? 'project'
        : worktreeGroupingActive
          ? 'projects'
          : 'flat'

  // Each reorderable list reports its OWN new id order; persisting is a direct,
  // typed write — no id-prefix sniffing to figure out which level moved.
  const reorderSessions = (ids: string[]) => {
    setSidebarSessionOrderManual(true)
    setSidebarSessionOrderIds(ids)
  }

  // Persist the new project overview order (drag-to-reorder); orderByIds applies
  // it over the default sort, so stale/new ids reconcile on the next render.
  const reorderProjects = (ids: string[]) => setSidebarProjectOrderIds(ids)

  // Sortable rows carry live session ids; the pinned store is keyed by durable
  // (lineage-root) ids, so translate before persisting the new order.
  const reorderPinned = (ids: string[]) =>
    setPinnedSessionOrder(
      ids.map(id => {
        const session = sessionByAnyId.get(id)

        return session ? sessionPinId(session) : id
      })
    )

  return (
    <Sidebar
      className={cn(
        // Visibility is the layout tree's job (a hidden zone is display:none;
        // the narrow overlay renders the live instance) — the sidebar always
        // paints itself fully.
        'relative h-full min-w-0 overflow-hidden border-t-0 border-b-0 text-foreground transition-none',
        panesFlipped ? 'border-l border-r-0' : 'border-r border-l-0',
        'border-(--sidebar-edge-border) bg-(--ui-sidebar-surface-background) opacity-100'
      )}
      collapsible="none"
      data-tip-region=""
      data-tour="sessions-sidebar"
    >
      <SidebarContent className="gap-0 overflow-hidden bg-transparent px-2.5">
        <SidebarGroup className="shrink-0 p-0 pb-2 pt-[calc(var(--titlebar-height)+0.375rem)]">
          <SidebarGroupContent>
            <SidebarMenu className="gap-px">
              {(navCollapsed ? navItems.filter(item => item.id === 'new-session') : navItems).map(item => {
                const isInteractive = Boolean(item.action) || Boolean(item.route)

                const active =
                  (item.id === 'capabilities' && currentView === 'capabilities') ||
                  (item.id === 'messaging' && currentView === 'messaging') ||
                  (item.id === 'artifacts' && currentView === 'artifacts') ||
                  (item.id === 'cron' && currentView === 'cron') ||
                  // Contributed rows light up at their own route.
                  (currentView === 'extension' && Boolean(item.route) && pathname === item.route)

                const isNewSession = item.id === 'new-session'

                const button = (
                  <SidebarMenuButton
                    aria-disabled={!isInteractive}
                    className={cn(
                      // no-drag: these rows sit directly under the titlebar's
                      // [-webkit-app-region:drag] strips (app-shell.tsx), with only
                      // 6px of clearance. Drag regions win hit-testing over DOM
                      // (pointer-events can't override), and on Linux/WSLg the
                      // resolved region has been observed to swallow clicks on the
                      // top rows. Same carve-out as USER_BUBBLE_BASE_CLASS in
                      // thread.tsx.
                      'flex h-7 w-full justify-start gap-2 rounded-md border border-transparent px-2 text-left text-[0.8125rem] font-medium text-(--ui-text-secondary) transition-colors duration-100 ease-out [-webkit-app-region:no-drag] hover:bg-(--ui-control-hover-background) hover:text-foreground hover:transition-none',
                      active &&
                        'border-(--ui-stroke-tertiary) bg-(--ui-control-active-background) text-foreground shadow-none hover:border-(--ui-stroke-tertiary)!',
                      !isInteractive &&
                        'cursor-default hover:border-transparent hover:bg-transparent hover:text-inherit'
                    )}
                    // A tip anchored to the label points at the end of the
                    // word; the row is what it's actually about.
                    data-tip-region=""
                    onClick={() => {
                      // A plain new session lands in whatever profile the live
                      // gateway is on (= the active switcher context). null →
                      // no swap. The switcher header is the single place to
                      // change which profile that is.
                      if (isNewSession) {
                        $newChatProfile.set(null)
                      }

                      if (item.keybindActionId) {
                        recordAction(item.keybindActionId, 'click')
                      }

                      onNavigate(item)
                    }}
                    onPointerDown={event => {
                      // The "New session" row is a drag source too: drag it onto
                      // a chat zone's tab strip / edge / center to create the
                      // session exactly there (stack / split). The pointer drag
                      // session owns the gesture — a sub-threshold release falls
                      // through to the onClick above (ordinary new session), and
                      // an engaged drag suppresses that click so it never
                      // double-creates. The create callback sets $newChatProfile
                      // itself (the suppressed click can't), so a dragged new
                      // session lands in the same profile a click would.
                      if (!isNewSession) {
                        return
                      }

                      startNewSessionDrag(placement => {
                        $newChatProfile.set(null)
                        onNewSessionSplit(placement.dir, {
                          anchor: placement.anchor,
                          before: placement.before
                        })
                      }, event)
                    }}
                    tooltip={
                      item.keybindActionId
                        ? {
                            children: (
                              <TipKeybindLabel actionId={item.keybindActionId} text={s.nav[item.id] ?? item.label} />
                            )
                          }
                        : (s.nav[item.id] ?? item.label)
                    }
                    type="button"
                  >
                    <item.icon className="size-4 shrink-0 text-[color-mix(in_srgb,currentColor_72%,transparent)]" />
                    {/* Shrink-to-fit, not flex-1: the label carries the row's
                        `data-tour` handle, and anything anchored to it should
                        land at the end of the WORD, not out at the sidebar's
                        edge. Still truncates — `min-w-0` lets it shrink past
                        its content when the rail is narrow — and the trailing
                        chip's `ml-auto` was already doing the pushing that
                        `flex-1` looked like it was for.
                        Its own `sidebar-nav-` namespace: the overlay nav owns
                        `nav-<id>`, and both are on screen with Settings open. */}
                    <span className="min-w-0 truncate" data-tip-arrow-only="" data-tour={`sidebar-nav-${item.id}`}>
                      {s.nav[item.id] ?? item.label}
                    </span>
                    {isNewSession && (
                      <>
                        <button
                          aria-expanded={!navCollapsed}
                          aria-label={navCollapsed ? 'Expand nav' : 'Collapse nav'}
                          className="ml-auto flex size-5 items-center justify-center rounded text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground [-webkit-app-region:no-drag]"
                          onClick={event => {
                            event.stopPropagation()
                            event.preventDefault()
                            toggleNavCollapsed()
                          }}
                          onPointerDown={event => {
                            // Keep the new-session drag source from engaging on the chevron.
                            event.stopPropagation()
                          }}
                          type="button"
                        >
                          <Codicon className="size-3.5" name={navCollapsed ? 'chevron-down' : 'chevron-up'} />
                        </button>
                        <KbdGroup
                          className={cn('opacity-55', newSessionKbdFlash && 'opacity-100!')}
                          keys={newSessionKbd}
                          size="sm"
                        />
                      </>
                    )}
                  </SidebarMenuButton>
                )

                // New session + route-backed pages can open in a split —
                // right-click for the directional "Open in split" submenu.
                return (
                  <SidebarMenuItem key={item.id}>
                    {isNewSession || item.route ? (
                      <ContextMenu>
                        <ContextMenuTrigger asChild>{button}</ContextMenuTrigger>
                        <ContextMenuContent aria-label={s.nav[item.id] ?? item.label}>
                          <SplitSubmenu
                            kit={CONTEXT_SPLIT_KIT}
                            label={s.row.openInSplit}
                            onSplit={dir => {
                              if (isNewSession) {
                                onNewSessionSplit(dir)
                              } else if (item.route) {
                                openRouteTile(item.route, dir)
                              }
                            }}
                          />
                        </ContextMenuContent>
                      </ContextMenu>
                    ) : (
                      button
                    )}
                  </SidebarMenuItem>
                )
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarStorageCorruptNotice />

        {showSessionSections && (
          <div className="shrink-0 px-2 pb-1 pt-1">
            <SearchField
              aria-label={s.searchAria}
              inputRef={searchInputRef}
              onChange={setSearchQuery}
              placeholder={s.searchPlaceholder}
              value={searchQuery}
            />
          </div>
        )}

        {showSessionSections && (
          <div
            className={cn('flex min-h-0 flex-1 flex-col pb-1.75', SCROLL_Y, SCROLL_GUTTER)}
            data-sessions-mode={sessionsMode}
            data-sessions-project={inProject ? (enteredProjectId ?? undefined) : undefined}
          >
            <SidebarSearchFailure label={s.searchFailedRetry} retry={retrySearch} visible={searchFailed} />
            {trimmedQuery && (
              <SidebarSessionsSection
                activeSessionId={activeSidebarSessionId}
                contentClassName={cn('flex min-h-0 flex-1 flex-col gap-px pb-1.75', SCROLL_Y)}
                emptyState={<SidebarSearchEmpty label={s.noMatch(trimmedQuery)} pending={searchPending} />}
                label={s.results}
                onArchiveSession={onArchiveSession}
                onBranchSession={onBranchSession}
                onDeleteSession={onDeleteSession}
                onResumeSession={onResumeSession}
                onToggle={() => undefined}
                onTogglePin={pinSession}
                onToggleUnread={toggleUnread}
                open
                pinned={false}
                preserveOrder
                rootClassName="min-h-32 flex-1 overflow-hidden p-0"
                sessions={searchResults}
                showProfileTags={showAllProfiles}
              />
            )}

            {!trimmedQuery && (
              <SidebarPinnedAndBackgroundSections
                activeSessionId={activeSidebarSessionId}
                // Pins and background owners share the active row geometry.
                card={cardRows}
                contentClassName="flex flex-col gap-px rounded-lg pb-2 pt-1"
                dndSensors={dndSensors}
                emptyState={<SidebarPinnedEmptyState archived={showArchived} filtered={filtersNarrow} />}
                label={s.pinned}
                onArchiveSession={onArchiveSession}
                onBranchSession={onBranchSession}
                onDeleteSession={onDeleteSession}
                onReorderSessions={reorderPinned}
                onResumeSession={onResumeSession}
                onToggle={() => setSidebarPinsOpen(!pinsOpen)}
                onTogglePin={unpinSession}
                onToggleUnread={toggleUnread}
                open={pinsOpen}
                pinned
                rootClassName="shrink-0 p-0 pb-1"
                sessions={pinnedSessions}
                showProfileTags={showAllProfiles}
                sortable={pinnedSessions.length > 1}
              />
            )}

            {!trimmedQuery && <SidebarDraftSections {...{ projectFilter, enteredProjectId, showArchived }} />}

            {!trimmedQuery && inProject && projectLoadFailed && <SidebarLoadErrorState onRetry={retryProject} />}
            {!trimmedQuery && (
              <SidebarSessionsSection
                activeProjectId={activeProjectId}
                activeSessionId={activeSidebarSessionId}
                // Inbox style is a render variant, not a grouping — it rides
                // whichever view is active: flat recents, project lanes, and
                // the overview previews all render the same card.
                card={cardRows}
                collapsible={!inProject}
                contentClassName={cn(
                  'flex min-h-0 flex-1 flex-col gap-px pb-1.75',
                  // The section is the ONE authority on whether the virtual
                  // list owns scrolling: it neutralizes this wrapper scroller
                  // itself (overflow-visible) when it virtualizes. Gating
                  // SCROLL_Y here on index's own parallel guess desynced the
                  // two — a cached project tree flipped this side but not the
                  // section's, leaving the list with no scroller at all and
                  // the recents pane rendering blank under Updated grouping.
                  SCROLL_Y,
                  // Flatten into the single scroll when compact — unless this is the
                  // virtualized long list, which must keep its own scroller.
                  !recentsVirtualizes && COMPACT_FLAT
                )}
                dndSensors={dndSensors}
                emptyState={
                  inProject && projectLoadFailed ? null : showSessionSkeletons || (inProject && projectLoading) ? (
                    <SidebarSessionSkeletons />
                  ) : !inProject && sessionsLoadError ? (
                    <SidebarLoadErrorState onRetry={() => void onRetrySessions()} />
                  ) : (
                    <div className="grid min-h-16 place-items-center rounded-lg px-2 text-center text-xs text-(--ui-text-tertiary)">
                      {inProject
                        ? s.projectEmpty
                        : filtersActive
                          ? s.noFilterMatches
                          : pinnedSessions.length > 0
                            ? s.allPinned
                            : s.noSessions}
                    </div>
                  )
                }
                footer={
                  // Hidden only when workspace-grouped — those groups page
                  // themselves. Profile groups don't: this one footer fetches the
                  // next page, which grows every profile at once. Archived is
                  // flat whatever the grouping, so it always pages here.
                  archive.pick(true, !agentsGrouped) && !showSessionSkeletons && hasMoreSessions ? (
                    <SidebarLoadMoreRow
                      loading={archive.pick(archive.loadingMore, sessionsLoading || recentsLoadMorePending)}
                      onClick={() => void archive.pick(archive.loadMore, onLoadMoreRecents)()}
                      // Recents are post-filtered to non-project sessions, so a
                      // backend page size (50) is not a truthful "rows you'll
                      // see" count. Use the generic label instead of a fake N.
                      step={0}
                    />
                  ) : null
                }
                forceEmptyState={showSessionSkeletons}
                // Archived is a plain list, and so is a magnitude-ranked one.
                // Otherwise project lanes stay chronological whatever the flat
                // list does — only the flat list can swap its dividers for
                // WORKING / DONE.
                grouping={showArchived || rankedGlobally ? 'none' : grouping === 'status' ? 'status' : 'date'}
                groups={displayAgentGroups}
                headerAction={
                  // One cluster, not a fragment: the header is justify-between,
                  // so two children (mark-all + the rest) park the check-all in
                  // the middle as a blank 24px hole until hover.
                  <div className="flex shrink-0 items-center gap-0.5">
                    {unreadCount > 0 && (
                      <Tip label={s.markAllRead}>
                        <Button
                          aria-label={s.markAllRead}
                          className={HEADER_ACTION_BTN}
                          onClick={event => {
                            event.stopPropagation()
                            markAllSessionsRead()
                            // Ack the persisted layer too, or the next list
                            // refresh repaints every dot just dismissed.
                            ackAllSessionsRead()
                          }}
                          size="icon-xs"
                          variant="ghost"
                        >
                          <Codicon name="check-all" size="0.75rem" />
                        </Button>
                      </Tip>
                    )}
                    {inProject && enteredProject ? (
                      <div className="group/workspace flex shrink-0 items-center gap-0.5">
                        {enteredProject.path && <StartWorkButton project={enteredProject} />}
                        {/* Home has no folder and no record to rename, theme, or delete. */}
                        {!enteredProject.isNoProject && (
                          <EnteredPresentationMenu
                            isActive={enteredProject.id === activeProjectId}
                            members={groupedEnteredMembers}
                            onExitScope={exitProjectScope}
                            project={enteredProject}
                            scoped
                          />
                        )}
                        <div className="grid size-6 place-items-center">
                          <Tip label={s.showProjects}>
                            <Button
                              aria-label={s.showProjects}
                              className={HEADER_NAV_BTN}
                              onClick={event => {
                                event.stopPropagation()
                                exitProjectScope()
                              }}
                              size="icon-xs"
                              variant="ghost"
                            >
                              <Codicon name="list-unordered" size="0.75rem" />
                            </Button>
                          </Tip>
                        </div>
                      </div>
                    ) : (
                      <>
                        {/* The flat-list header "+" is a drag source too — the
                            same gesture as the nav's "New session" row: drag
                            it onto a chat zone's tab strip / edge / center to
                            create the session exactly there. Project-overview
                            mode drags its "+" (the "New project" button) with
                            the project-drag variant: a drop opens the SAME
                            project dialog, and the created project starts at
                            the dropped spot. */}
                        <SidebarSectionAddButton
                          ariaLabel={agentsGrouped ? s.projects.newButton : s.nav['new-session']}
                          onNewProjectDrag={
                            agentsGrouped
                              ? {
                                  // Dragging the "New project" + arms WHERE the
                                  // project should start; the dialog flow consumes
                                  // it on create (see $newProjectDropPlacement).
                                  onArm: placement => $newProjectDropPlacement.set(placement)
                                }
                              : undefined
                          }
                          onNewSessionSplit={agentsGrouped ? undefined : onNewSessionSplit}
                          onPlainClick={() => {
                            if (agentsGrouped) {
                              openProjectCreate()
                            } else {
                              onNewSessionInWorkspace(null)
                            }
                          }}
                        />
                        <div className="grid size-6 place-items-center">
                          <SidebarFilterMenu className={HEADER_NAV_BTN} projectGroups={navigationGroups} />
                        </div>
                      </>
                    )}
                  </div>
                }
                label={sessionsLabel}
                labelMeta={
                  worktreeGroupingActive ? (
                    reposScanning && !projectsSkeletonVisible ? (
                      <GlyphSpinner ariaLabel={s.loading} className="text-[0.6875rem] text-(--ui-text-quaternary)" />
                    ) : undefined
                  ) : undefined
                }
                liveSessions={inProject ? enteredProjectOverlaySessions : undefined}
                manualOrderIds={agentOrderManual ? agentOrderIds : sortOrderIds}
                onArchiveSession={onArchiveSession}
                onBranchSession={onBranchSession}
                onDeleteSession={onDeleteSession}
                onEnterProject={onEnterProject}
                onNewSessionInWorkspace={onNewSessionInWorkspace}
                onNewSessionSplit={onNewSessionSplit}
                onReorderProjects={projectPolicy.reorderHandler(showAllProfiles, reorderProjects)}
                onReorderSessions={showAllProfiles ? undefined : reorderSessions}
                onResumeSession={onResumeSession}
                onToggle={() => setSidebarRecentsOpen(!agentsOpen)}
                onTogglePin={pinSession}
                onToggleUnread={toggleUnread}
                open={agentsOpen}
                pinned={false}
                projectBackRow={
                  inProject ? <ProjectBackRow label={s.projects.back} onClick={exitProjectScope} /> : undefined
                }
                projectContent={inProject ? enteredProjectContent : undefined}
                projectMemberBody={
                  groupedEnteredMembers
                    ? renderRows => (
                        <EnteredProjectMembers
                          members={groupedEnteredMembers}
                          onNewSession={onNewSessionInWorkspace}
                          onNewSessionSplit={onNewSessionSplit}
                          options={enteredMemberOptions}
                          primary={primaryMember}
                          renderRows={renderRows}
                        />
                      )
                    : undefined
                }
                projectOverview={projectOverview}
                projectOverviewHidden={overviewHidden}
                projectOverviewPreviews={overviewPreviews}
                projectRepoWorktrees={inProject ? scopedRepoWorktrees : undefined}
                projectsLoading={worktreeGroupingActive ? projectTreeLoading : false}
                removedSessionIds={inProject ? removedSessionIds : undefined}
                rootClassName={cn(
                  'min-h-32 flex-1 overflow-hidden p-0',
                  !recentsVirtualizes && 'compact:min-h-0 compact:flex-none compact:overflow-visible'
                )}
                sessions={displayAgentSessions}
                sortable={!showAllProfiles && agentSessions.length > 1}
              />
            )}

            {!trimmedQuery &&
              !worktreeGroupingActive &&
              messagingGroups.map(group => {
                const visible = messagingVisible[group.sourceId] ?? NON_SESSION_INITIAL_ROWS
                const shownSessions = group.sessions.slice(0, visible)
                // More to show if rows are hidden behind the cap, or the backend
                // still has older threads on disk.
                const canRevealMore = visible < group.sessions.length || group.hasMore

                // Group only what the cap lets through, so the footer's count
                // and load-more stay about the platform, not one of its groups.
                const ownerGroups = ownerGrouped
                  ? scopeGatewaySessionGroups(
                      buildGatewaySessionGroups(shownSessions, connectionsRegistry, profileColors),
                      `messaging:${group.sourceId}`
                    )
                  : undefined

                return (
                  <SidebarSessionsSection
                    activeSessionId={activeSidebarSessionId}
                    contentClassName={cn('flex max-h-56 flex-col gap-px pb-1.75', GROUP_BODY)}
                    embeddedGroups
                    emptyState={null}
                    footer={
                      canRevealMore ? (
                        <SidebarLoadMoreRow
                          loading={Boolean(messagingLoadMorePending[group.sourceId])}
                          onClick={() => revealMoreMessaging(group.sourceId, group.sessions.length, group.hasMore)}
                          step={Math.min(NON_SESSION_LOAD_STEP, Math.max(0, group.total - shownSessions.length))}
                        />
                      ) : null
                    }
                    groups={ownerGroups}
                    key={group.sourceId}
                    label={group.label}
                    labelIcon={
                      <PlatformAvatar
                        className="size-4 rounded-[4px] text-[0.5625rem] [&_svg]:size-3"
                        platformId={group.sourceId}
                        platformName={group.label}
                      />
                    }
                    onArchiveSession={onArchiveSession}
                    onDeleteSession={onDeleteSession}
                    onResumeSession={onResumeSession}
                    onToggle={() => toggleSidebarMessagingOpen(group.sourceId)}
                    onTogglePin={pinSession}
                    onToggleUnread={toggleUnread}
                    open={messagingOpenIds.includes(group.sourceId)}
                    pinned={false}
                    rootClassName="shrink-0 p-0"
                    sessions={shownSessions}
                    showProfileTags={showAllProfiles && !ownerGrouped}
                  />
                )
              })}

            {!trimmedQuery && !worktreeGroupingActive && showsAdvancedChrome && cronJobs.length > 0 && (
              <SidebarCronJobsSection
                jobs={cronJobs}
                label={s.cronJobs}
                onManageJob={onManageCronJob}
                onOpenRun={onResumeSession}
                onToggle={() => setSidebarCronOpen(!cronOpen)}
                onTriggerJob={onTriggerCronJob}
                open={cronOpen}
              />
            )}
          </div>
        )}

        {!showSessionSections && <SidebarBlankState onNewProject={openProjectCreate} />}

        {/* Off, the statusbar's profile dropdown (beside the gateway switcher)
            takes over — the rail is a duplicate door for bot-only setups. */}
        {profileRailVisible && (
          <div className="shrink-0 px-0.5 pb-1 pt-0.5">
            <ProfileRail />
          </div>
        )}
      </SidebarContent>
      <ProjectDialog />
      <GlobalSessionRenameDialog />
      {/* One mount for the whole app. The header of WorktreeDialog tells why. */}
      <WorktreeDialog />
    </Sidebar>
  )
}

interface MessagingSection {
  sourceId: string
  label: string
  sessions: SessionInfo[]
  total: number
  hasMore: boolean
}
