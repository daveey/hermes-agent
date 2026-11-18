import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'

import { $apiRequestScope } from '@/api/client'
import { getApiRequestConnection, getSession, listAllProfileSessions, listSidebarSessions, type SessionInfo } from '@/hermes'
import { sameCronSignature } from '@/lib/session-signatures'
import {
  isMessagingSource,
  LOCAL_SESSION_SOURCE_IDS,
  normalizeSessionSource,
  SIDEBAR_EXCLUDED_SOURCES
} from '@/lib/session-source'
import { markSidebarLive } from '@/store/boot-snapshot'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { gatewayActivationEpoch, pinnedPrimaryGatewayConnectionId, primaryGatewayConnectionId } from '@/store/gateway'
import {
  $pinnedSessionIds,
  $sessionsLimit,
  $sidebarFiltersActive,
  bumpSessionsLimit,
  raiseSessionsLimit,
  SIDEBAR_FILTERED_PAGE_SIZE,
  SIDEBAR_SESSIONS_PAGE_SIZE
} from '@/store/layout'
import { $sessionListFreshness, recordSessionListResult, sessionOwnerIsFresh, staleSessionOwners } from '@/store/offline-sessions'
import { messagingTotalsKey, normalizeProfileKey, sidebarProfileForScope } from '@/store/profile'
import {
  $messagingSessions,
  $selectedStoredSessionId,
  $sessions,
  carryForwardFailedProfileSessions,
  CRON_SECTION_LIMIT,
  keepFailedProfileMeta,
  mergeSessionPage,
  MESSAGING_SECTION_LIMIT,
  messagingListServerForFetch,
  setCorruptSessionStores,
  setCronSessions,
  setMessagingListServer,
  setMessagingPlatformTotals,
  setMessagingSessions,
  setMessagingTruncated,
  setSessionProfilesFailed,
  setSessionProfilesTruncated,
  setSessionProfilesUsage,
  setSessions,
  setSessionsLoadError,
  setSessionsLoading,
  stampMessagingRowsWithListServer
} from '@/store/session'
import { migrateLocalParks } from '@/store/session-blocked'
import {
  markSessionListRefreshReason,
  noteSessionListSource,
  takeSessionListRefreshReason
} from '@/store/session-dot-state'
import {
  $removedSessionIds,
  captureSessionTombstoneGenerations,
  sessionRemovalIntersected,
  type SessionTombstoneGenerationSnapshot,
  tombstoneRowIds
} from '@/store/session-removal'
import { $sessionTiles, $workingSessionIds, getRecentlySettledSessionIds } from '@/store/session-states'

import { refreshCronJobs as refreshCronJobsStore } from '../../cron/cron-actions'

import { ownRecentsConnection, withPrimaryRecents } from './primary-recents'
import { refreshRetainedKanban, withKanbanEvidence } from './refresh-retained-kanban'

/** [daveey] Backoff for re-listing while a failed list leaves owners stale (last value repeats). */
export const STALE_LIST_RETRY_MS = [15_000, 30_000, 60_000]

function sidebarRequestConnection(): string | undefined {
  return getApiRequestConnection() ?? undefined
}

function recordSidebarFailure(owns: () => boolean, connection: string | undefined, profile: string): void {
  if (owns()) {recordSessionListResult($sessions.get(), [], connection, profile, [], true)}
}

function recordSidebarFreshness(result: Awaited<ReturnType<typeof listSidebarSessions>>, connection: string | undefined, profile: string): void {
  const recents = result.recents
  const recentsErrors = recents.errors ?? result.errors
          recordSessionListResult(
            $sessions.get(),
            recents.sessions ?? [],
            connection,
            profile,
            [
              ...(recentsErrors ?? []),
              ...Object.entries(recents.profiles_failed ?? result.profiles_failed ?? {})
                .filter(([, failure]) => failure.failed || failure.retry)
                .map(([profile]) => ({ profile }))
            ],
            recents.failed === true || recents.retry === true
          )

  // [daveey] Cold-start timing: the first live list this launch.
  if (recents.failed !== true && recents.retry !== true) {
    markSidebarLive()
  }
}

// The messaging slice is the inverse: drop cron + every local source so only
// external-platform conversations remain, then split per platform in the UI.
const MESSAGING_EXCLUDED_SOURCES = ['cron', ...LOCAL_SESSION_SOURCE_IDS]

// Drop rows the user just deleted/archived: ANY list fetch (full refresh,
// "Load more" paging, a per-platform messaging page, the cron slice) can race
// an in-flight delete RPC, and the backend page still carries the doomed row
// until the DELETE commits — so it flashed back into the sidebar (#50928).
// Honoring the optimistic tombstone at every ingestion point keeps the removal
// stable; the tombstone self-clears once projects.tree confirms the delete,
// and a failed delete untombstones immediately, so nothing is filtered on the
// non-destructive paths. A tombstone matches the row on ANY id the
// conversation has answered to (tip, root, and every intermediate lineage
// segment) — armed on one name, it must still catch the same conversation
// returning under another (#123685).
function dropTombstoned(sessions: SessionInfo[]): SessionInfo[] {
  const tombstones = $removedSessionIds.get()

  if (!tombstones.size) {
    return sessions
  }

  const tombstoned = (session: SessionInfo): boolean => tombstoneRowIds(session).some(id => tombstones.has(id))

  const kept = sessions.filter(session => !tombstoned(session))

  return kept.length === sessions.length ? sessions : kept
}

// A fetch whose page was READ before an archive/delete committed can land
// after the projects.tree prune has already dropped the tombstone (the tree is
// right to prune — the id is gone from its snapshot), so `dropTombstoned` has
// nothing left to honor and the stale row resurrected through the keep set
// (#123685). The tombstone's generation counter survives the prune: capture it
// at fetch start, and reject any row whose removal lifecycle moved toward
// removal underneath the request. A release edge (failed RPC, unarchive)
// re-admits the row as before.
function dropRemovalRaced(sessions: SessionInfo[], snapshot: SessionTombstoneGenerationSnapshot): SessionInfo[] {
  const raced = (session: SessionInfo): boolean =>
    tombstoneRowIds(session).some(id => sessionRemovalIntersected(snapshot, id))

  const kept = sessions.filter(session => !raced(session))

  return kept.length === sessions.length ? sessions : kept
}

function publishMessagingRows(rows: SessionInfo[], scopeProfile: string): SessionInfo[] {
  const server = messagingListServerForFetch(scopeProfile, getApiRequestConnection())
  setMessagingListServer(server)

  return stampMessagingRowsWithListServer(rows, server)
}

// Rows a session refresh must preserve even if the aggregator omits them:
// in-flight first turns (message_count 0), pinned rows aged off the page, the
// actively-viewed chat (its "working" flag clears a beat before the aggregator
// sees the persisted row), and sessions whose turn just settled (same race, but
// for a chat the user has already navigated away from). Pass `scope` to only
// keep the active row when it belongs to the profile being paged.
function sessionsToKeep(scope?: string): Set<string> {
  const keep = new Set<string>([
    ...$workingSessionIds.get(),
    ...$pinnedSessionIds.get(),
    ...getRecentlySettledSessionIds()
  ])

  // Open tiles are user-visible state exactly like the selected row: a branch
  // child is a DRAFT until its first real turn, so the aggregator can't return
  // it — without this the next background refresh silently dropped the
  // optimistic `draft: branch #N` row while its tab was open, and the sidebar
  // showed no trace of the branch until first send.
  for (const tile of $sessionTiles.get()) {
    keep.add(tile.storedSessionId)
  }

  const active = $selectedStoredSessionId.get()

  if (active) {
    const session = scope ? $sessions.get().find(s => s.id === active) : null

    if (!scope || !session || normalizeProfileKey(session.profile) === scope) {
      keep.add(active)
    }
  }

  return keep
}

interface UseSessionListActionsArgs {
  profileScope: string
}

/** Owns the sidebar's session-list fetching + paging: recents, cron runs/jobs,
 *  and the per-platform messaging slices. Returns the callbacks the controller
 *  wires into the sidebar and refresh effects. */
export function useSessionListActions({ profileScope }: UseSessionListActionsArgs) {
  const profileScopeRef = useRef(profileScope)
  const loadMoreMessagingRequestRef = useRef<Record<string, number>>({})
  const refreshMessagingSessionsRequestRef = useRef(0)
  const refreshSessionsRequestRef = useRef(0)

  useLayoutEffect(() => {
    profileScopeRef.current = profileScope
  }, [profileScope])

  /** Refresh the active profile's messaging-platform sidebar slice. */
  const refreshMessagingSessions = useCallback(async () => {
    const sessionProfile = sidebarProfileForScope(profileScope)

    // A callback captured before a profile switch may still be queued by an
    // event subscription. Do not let it start a request against the old scope.
    if (sidebarProfileForScope(profileScopeRef.current) !== sessionProfile) {
      return
    }

    const requestId = refreshMessagingSessionsRequestRef.current + 1
    refreshMessagingSessionsRequestRef.current = requestId

    // Same removal-race guard as the recents refresh (#123685).
    const removalSnapshot = captureSessionTombstoneGenerations()

    const owns = () =>
      refreshMessagingSessionsRequestRef.current === requestId &&
      sidebarProfileForScope(profileScopeRef.current) === sessionProfile

    const fetchPage = () =>
      listAllProfileSessions(MESSAGING_SECTION_LIMIT, 1, 'exclude', 'recent', sessionProfile, {
        excludeSources: MESSAGING_EXCLUDED_SOURCES
      })

    try {
      let activationEpoch = gatewayActivationEpoch()
      let result = await fetchPage()

      // Same re-read as refreshSessions: a mid-request activation voids this
      // page, and nothing else asks again when the route atoms did not move.
      while (owns() && gatewayActivationEpoch() !== activationEpoch) {
        activationEpoch = gatewayActivationEpoch()
        result = await fetchPage()
      }

      if (!owns()) {
        return
      }

      // Drop any non-messaging source the broad exclude didn't catch (custom
      // sources) — those stay in local recents, not a platform section.
      const rows = publishMessagingRows(
        dropRemovalRaced(dropTombstoned(result.sessions.filter(s => isMessagingSource(s.source))), removalSnapshot),
        sessionProfile
      )

      setMessagingSessions(prev => (sameCronSignature(prev, rows) ? prev : rows))
      // Hit the cap → at least one platform may have more on disk than loaded,
      // so platform sections offer their own per-platform "load more".
      setMessagingTruncated(result.sessions.length >= MESSAGING_SECTION_LIMIT)
    } catch {
      // Non-fatal: the messaging sections just stay empty/stale.
    }
  }, [profileScope])

  /** Page one messaging platform without replacing another platform's rows. */
  const loadMoreMessagingForPlatform = useCallback(
    async (platform: string) => {
      const sessionProfile = sidebarProfileForScope(profileScope)

      if (sidebarProfileForScope(profileScopeRef.current) !== sessionProfile) {
        return
      }

      const requestKey = messagingTotalsKey(sessionProfile, platform)
      const requestId = (loadMoreMessagingRequestRef.current[requestKey] ?? 0) + 1
      loadMoreMessagingRequestRef.current[requestKey] = requestId

      const inProfile = (s: SessionInfo) =>
        sessionProfile === 'all' || normalizeProfileKey(s.profile) === sessionProfile

      const inPlatform = (s: SessionInfo) => normalizeSessionSource(s.source) === platform && inProfile(s)
      const loaded = $messagingSessions.get().filter(inPlatform).length

      // Same removal-race guard as the recents refresh: this page was read
      // before an archive could commit and can outlive the tombstone prune.
      const removalSnapshot = captureSessionTombstoneGenerations()

      const owns = () =>
        loadMoreMessagingRequestRef.current[requestKey] === requestId &&
        sidebarProfileForScope(profileScopeRef.current) === sessionProfile

      const fetchPage = () =>
        listAllProfileSessions(loaded + SIDEBAR_SESSIONS_PAGE_SIZE, 1, 'exclude', 'recent', sessionProfile, {
          source: platform
        })

      let result

      try {
        let activationEpoch = gatewayActivationEpoch()
        result = await fetchPage()

        while (owns() && gatewayActivationEpoch() !== activationEpoch) {
          activationEpoch = gatewayActivationEpoch()
          result = await fetchPage()
        }
      } catch {
        // Non-fatal: leave the platform's loaded rows and total unchanged.
        return
      }

      if (!owns()) {
        return
      }

      const incoming = publishMessagingRows(
        dropRemovalRaced(dropTombstoned(result.sessions.filter(inPlatform)), removalSnapshot),
        sessionProfile
      )

      setMessagingSessions(prev => [
        ...prev.filter(s => !inPlatform(s)),
        ...mergeSessionPage(
          prev.filter(inPlatform),
          carryForwardFailedProfileSessions(prev.filter(inPlatform), incoming, result.errors),
          sessionsToKeep()
        )
      ])

      const total = result.total ?? incoming.length

      setMessagingPlatformTotals(prev => ({ ...prev, [requestKey]: Math.max(total, incoming.length) }))
    },
    [profileScope]
  )

  /** Refresh cron jobs only while the profile that requested them remains active. */
  const refreshCronJobs = useCallback(async () => {
    const sessionProfile = sidebarProfileForScope(profileScope)

    if (sidebarProfileForScope(profileScopeRef.current) !== sessionProfile) {
      return
    }

    try {
      await refreshCronJobsStore(sessionProfile)
    } catch {
      // Non-fatal: the cron section just keeps its last-known jobs.
    }
  }, [profileScope])

  /** Refresh every sidebar session slice without committing an obsolete profile response. */
  const refreshSessions = useCallback(
    async (shouldPublish: () => boolean = () => true) => {
      const sessionProfile = sidebarProfileForScope(profileScope)

      if (!shouldPublish() || sidebarProfileForScope(profileScopeRef.current) !== sessionProfile) {
        return
      }

      const requestConnection = sidebarRequestConnection()
      const refreshReason = takeSessionListRefreshReason()
      const requestId = refreshSessionsRequestRef.current + 1
      refreshSessionsRequestRef.current = requestId
      // The loading flag exists to drive the initial skeletons (they only render
      // while the list is empty). Turn-complete / reconnect refreshes over a
      // populated list used to flip it true→false anyway, churning every
      // $sessionsLoading subscriber twice per turn for no visible change.
      const showLoading = $sessions.get().length === 0

      if (showLoading && shouldPublish()) {
        setSessionsLoadError(false)
        setSessionsLoading(true)
      }

      const owns = () =>
        shouldPublish() &&
        refreshSessionsRequestRef.current === requestId &&
        sidebarProfileForScope(profileScopeRef.current) === sessionProfile

      // Snapshot the removal lifecycle BEFORE the first read: a page read
      // pre-archive-commit can land post-prune, and only the generation
      // delta (not tombstone membership) still names the doomed row then.
      const removalSnapshot = captureSessionTombstoneGenerations()

      try {
        const limit = $sessionsLimit.get()

        // Require at least one message so abandoned/empty "Untitled" drafts (one
        // was created per TUI/desktop launch before the lazy-create fix) don't
        // clutter the sidebar.
        // Unified cross-profile list (served read-only off each profile's
        // state.db; no per-profile backend is spawned). Single-profile users get
        // the same rows tagged profile="default".
        // Scope every sidebar slice to the active profile (not always 'all') so a profile
        // with few recent sessions isn't windowed out of the cross-profile
        // recency page and never inherits another profile's cron or messaging
        // sections. ALL_PROFILES remains the explicit unified view.
        // Batched: one request opens each profile DB once and returns all three
        // source-scoped slices, instead of three separate listAllProfileSessions
        // calls that each reopened + re-counted every profile DB per refresh.
        const pageRequest = {
          recentsProfile: sessionProfile,
          recentsLimit: limit,
          recentsExclude: SIDEBAR_EXCLUDED_SOURCES,
          cronLimit: CRON_SECTION_LIMIT,
          messagingLimit: MESSAGING_SECTION_LIMIT,
          messagingExclude: MESSAGING_EXCLUDED_SOURCES
        }

        // [daveey] A window on another gateway still lists this Mac's own chats live (see primary-recents.ts).
        const fetchPage = () => {
          const own = ownRecentsConnection($connectionsRegistry.get(), primaryGatewayConnectionId())

          return withPrimaryRecents(listSidebarSessions(pageRequest), requestConnection, own.connectionId, () =>
            listSidebarSessions({ ...pageRequest, connectionId: own.connectionId, passive: own.passive })
          )
        }

        let activationEpoch: number
        let result: Awaited<ReturnType<typeof fetchPage>>
        let primaryConnection: ReturnType<typeof primaryGatewayConnectionId>
        let refreshed: SessionInfo[] = []

        // Re-activating the same route moves no route atom, so no effect starts
        // another refresh. Retry the entire observation (page AND details) when
        // activation changes, unless a newer request/scope already owns it.
        do {
          activationEpoch = gatewayActivationEpoch()
          primaryConnection = primaryGatewayConnectionId()
          const primaryPin = pinnedPrimaryGatewayConnectionId()
          result = await fetchPage()

          if (!owns()) { return }

          if (gatewayActivationEpoch() !== activationEpoch) { continue }

          // Open/restored older rows survive paging, but otherwise their task
          // counts never refresh. Pin reads to the owner captured before await.
          const page = result.recents.sessions ?? []

          const retained = mergeSessionPage($sessions.get(), page, sessionsToKeep())
            .filter(row => !page.some(item => item.id === row.id &&
              (item.profile ?? 'default') === (row.profile ?? 'default') &&
              (item.connection_id ?? primaryConnection) === (row.connection_id ?? primaryConnection)))

          refreshed = await refreshRetainedKanban(retained, row => getSession(row.id, {
            connectionId: row.connection_id ?? primaryPin,
            profile: row.profile ?? 'default'
          }))
        } while (owns() && gatewayActivationEpoch() !== activationEpoch)

        if (owns() && gatewayActivationEpoch() === activationEpoch) {
          const recents = result.recents
          const recentsErrors = recents.errors ?? result.errors
          recordSidebarFreshness(result, requestConnection, sessionProfile)

          const scopedRetry =
            recents.retry === true ||
            (sessionProfile !== 'all' && recents.profiles_failed?.[sessionProfile]?.retry === true) ||
            result.profiles_failed?.[sessionProfile]?.retry === true

          setCorruptSessionStores(result.storage)
          noteSessionListSource(refreshReason)
          // A damaged store already has its own notice; Retry can't repair it.
          const retryableErrors = recentsErrors?.filter(e => !result.storage?.[e.profile])
          setSessionsLoadError(
            Boolean(showLoading && (scopedRetry || retryableErrors?.length) && (recents.sessions?.length ?? 0) === 0)
          )
          setSessionProfilesFailed(recentsErrors)

          // Drop rows the user just deleted/archived: a refresh can race an
          // in-flight mutation and the backend page still carries the doomed row.
          // Honoring the optimistic tombstone keeps the removal from flashing back
          // (the tombstone self-clears once projects.tree confirms the delete).
          // Signature-gate the swap (same pattern as cron/messaging): a refresh
          // that returns content-identical rows must keep the previous array
          // identity, or every sidebar memo keyed on $sessions recomputes and the
          // whole list re-renders once per turn/broadcast for nothing.
          setSessions(prev => {
            const incoming = dropTombstoned(
              carryForwardFailedProfileSessions(prev, recents.sessions ?? [], recents.errors ?? result.errors)
            )

            // Filter AFTER the merge: the guard must also catch survivors
            // (a stale previous slice can still hold the doomed row through
            // the keep set once the tombstone prune has cleared membership).
            const observed = prev.map(row => {
              const fresh = refreshed.find(item => item.id === row.id &&
                (item.profile ?? 'default') === (row.profile ?? 'default') &&
                (item.connection_id ?? primaryConnection) === (row.connection_id ?? primaryConnection))

              return fresh ? withKanbanEvidence(row, fresh) : row
            })

            const next = dropRemovalRaced(mergeSessionPage(observed, incoming, sessionsToKeep()), removalSnapshot)

            return sameCronSignature(prev, next) ? prev : next
          })
          // [daveey] First page from a backend that owns the ⌘B park: push the old local parks to it, once.
          void migrateLocalParks(recents.sessions)
          // "Is there another page?" instead of an exact total: the backend
          // reports which profiles filled their window, which costs nothing on
          // top of the rows it already read (the old exact totals ran a COUNT(*)
          // per profile DB on every refresh). Reference-stable when unchanged so
          // the sidebar's group memos don't recompute per refresh.
          setSessionProfilesTruncated(prev => {
            const next = keepFailedProfileMeta(prev, recents.profiles_truncated ?? {}, recentsErrors)
            const prevKeys = Object.keys(prev)

            return prevKeys.length === Object.keys(next).length && prevKeys.every(key => prev[key] === next[key])
              ? prev
              : next
          })
          // Same identity gate: these totals only move when a session bills, and
          // a fresh object every refresh would repaint every profile header.
          setSessionProfilesUsage(prev => {
            const next = keepFailedProfileMeta(prev, recents.profiles_usage ?? {}, recentsErrors)
            const prevKeys = Object.keys(prev)

            return prevKeys.length === Object.keys(next).length &&
              prevKeys.every(
                key => prev[key]?.tokens === next[key]?.tokens && prev[key]?.cost_usd === next[key]?.cost_usd
              )
              ? prev
              : next
          })

          // Cron section: latest N cron sessions (kept so a pinned cron run still
          // resolves via sessionByAnyId), signature-gated like above. The
          // optimistic tombstone applies here too — the batched page can carry
          // a cron run whose delete/archive RPC is still in flight (#50928).
          setCronSessions(prev => {
            const incoming = carryForwardFailedProfileSessions(
              prev,
              dropRemovalRaced(dropTombstoned(result.cron.sessions ?? []), removalSnapshot),
              result.cron.errors ?? result.errors
            )

            return sameCronSignature(prev, incoming) ? prev : incoming
          })

          // Messaging sections: drop any non-messaging source the broad exclude
          // didn't catch (custom sources stay in local recents), then split per
          // platform in the UI.
          const messagingErrors = result.messaging.errors ?? result.errors

          const messagingRows = publishMessagingRows(
            dropRemovalRaced(
              dropTombstoned(
                carryForwardFailedProfileSessions(
                  $messagingSessions.get(),
                  (result.messaging.sessions ?? []).filter(s => isMessagingSource(s.source)),
                  messagingErrors
                )
              ),
              removalSnapshot
            ),
            sessionProfile
          )

          setMessagingSessions(prev => (sameCronSignature(prev, messagingRows) ? prev : messagingRows))
          // Hit the cap → at least one platform may have more on disk than loaded.
          setMessagingTruncated(prev =>
            messagingErrors?.length ? prev : result.messaging.sessions.length >= MESSAGING_SECTION_LIMIT
          )
        }
      } catch (error) {
        recordSidebarFailure(owns, requestConnection, sessionProfile)

        if (owns() && showLoading) {
          setSessionsLoadError(true)
        }

        throw error
      } finally {
        // Request identity preserves the zero-argument refresh contract across a
        // failed activation epoch; an explicit owner predicate is stronger and
        // must never release a newer switch's loading barrier.
        if (showLoading && shouldPublish() && refreshSessionsRequestRef.current === requestId) {
          setSessionsLoading(false)
        }
      }

      // Cron *jobs* are a distinct API (getCronJobs), not a session slice.
      if (shouldPublish() && sidebarProfileForScope(profileScopeRef.current) === sessionProfile) {
        void refreshCronJobs()
      }
    },
    [profileScope, refreshCronJobs]
  )

  const loadMoreSessions = useCallback(async () => {
    bumpSessionsLimit()
    await refreshSessions()
  }, [refreshSessions])

  // A filter searches the loaded page, so switching one on has to deepen the
  // page — otherwise "merged PRs" answers for the last 50 rows and reads as
  // "you only have 6 merged PRs". Clearing the filters hands the window back:
  // the list refreshes on every settled turn, and paying for 300 rows a turn
  // once the view is unfiltered again buys nothing. Whatever the user had
  // paged to by hand is what it returns to.
  const unfilteredLimit = useRef<null | number>(null)

  useEffect(
    () =>
      $sidebarFiltersActive.subscribe(active => {
        if (active) {
          unfilteredLimit.current ??= $sessionsLimit.get()

          if (raiseSessionsLimit(SIDEBAR_FILTERED_PAGE_SIZE)) {
            void refreshSessions()
          }
        } else if (unfilteredLimit.current !== null) {
          const restored = unfilteredLimit.current
          unfilteredLimit.current = null

          if ($sessionsLimit.get() > restored) {
            $sessionsLimit.set(restored)
            void refreshSessions()
          }
        }
      }),
    [refreshSessions]
  )

  // [daveey] A failed list leaves its owners stale: their rows paint as cached copies (no red/grey stripe, 0/0 kanban
  // badge) and only a SUCCESSFUL list makes them live again. Nothing used to re-list on its own: after a Desktop
  // restart whose first hub list failed, this Mac's rows stayed blank for 16 min, until the user switched gateways.
  // Re-list with backoff while any owner is stale; any successful refresh (this one or another) ends it.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0

    const clear = () => {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    }

    const schedule = (state: Record<string, boolean>) => {
      // A probe wildcard needs a list of that gateway, not unrelated cached rows.
      // Scoped success repairs only that profile; keep other profiles fail-closed.
      const connection = sidebarRequestConnection()
      const sessionProfile = sidebarProfileForScope(profileScope)
      const wildcard = `${connection?.trim() || 'local'}/*`

      const eligible = staleSessionOwners(state).some(owner =>
        !owner.endsWith('/*') || (owner === wildcard &&
          (sessionProfile === 'all' || !sessionOwnerIsFresh(connection, sessionProfile, state))))

      if (!eligible) {
        attempt = 0
        clear()

        return
      }

      if (timer !== null) {
        return
      }

      const delay = STALE_LIST_RETRY_MS[Math.min(attempt, STALE_LIST_RETRY_MS.length - 1)]
      timer = setTimeout(() => {
        timer = null
        attempt += 1
        markSessionListRefreshReason('stale-owner-retry')
        void refreshSessions()
          .catch(() => undefined)
          .finally(() => schedule($sessionListFreshness.get()))
      }, delay)
    }

    const unsubscribe = $sessionListFreshness.subscribe(schedule)
    const unsubscribeScope = $apiRequestScope.subscribe(() => schedule($sessionListFreshness.get()))

    return () => {
      unsubscribeScope()
      unsubscribe()
      clear()
    }
  }, [refreshSessions, profileScope])

  return {
    loadMoreMessagingForPlatform,
    loadMoreSessions,
    refreshCronJobs,
    refreshMessagingSessions,
    refreshSessions
  }
}
