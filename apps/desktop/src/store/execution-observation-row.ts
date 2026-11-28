import { computed, type ReadableAtom } from 'nanostores'

import { summarizeExecutionObservations } from '@/lib/execution-observations'
import type { SessionInfo } from '@/types/hermes'

import { $backgroundStatusBySession } from './composer-status'
import { $activeGatewayRoute, activeGatewayProfileKey, gatewayRouteState, isActivePrimary } from './gateway'
import { $gatewayActivationEpoch } from './gateway-activation-epoch'
import { $liveWorkRoster, rosterRowEvidence, selectWorkObservations } from './live-work-roster'
import { $projectTree } from './project-tree'
import { $cronSessions, $messagingSessions, $sessions, $unlistedSessionOwnerRows, lineageAliases } from './session'
import { isSessionOwnerRoute } from './session-request-router'
import { $sessionStates, $sessionTiles, knownOwnerForSession } from './session-states'
import { $subagentsBySession } from './subagents'

/**
 * Sidebar consumer of the optional per-row `execution_observations` list (pure projector in
 * lib/execution-observations.ts; this module never re-implements its verdicts).
 *
 * Merge row observations with the matching owner-route roster and event-fed RPC snapshots. Legacy
 * counts discover work but missing per-kind evidence is explicit unknown, including on first render.
 * No observation, discovery or failed source => null (not proof of zero work).
 * Expiry is time-only: the projector is re-run against the render clock, so a cached running/waiting/needs_user entry
 * degrades to unknown with no fetch (the existing 15 s `$kanbanObservationTick` re-evaluates; no new timer).
 */
export interface RowExecution {
  /** current running or queued work */
  live: number
  /** current waiting (dependency, schedule, review, ...) */
  waiting: number
  /** current needs_user */
  needsUser: number
  /** entries with no usable current evidence (expired, malformed, unverified, ...). Visible, never zero work. */
  unknown: number

  /** structured human requests still outstanding, including ones retained by expired entries */
  outstanding: number
}

type Stamped = Pick<SessionInfo, 'execution_observations' | 'id' | 'connection_id' | 'profile'> &
  Partial<Pick<SessionInfo, 'kanban_active' | 'kanban_parked'>>

/** A producer that sent card counts but no `execution_observations` at all is an older backend (mixed
 * version): its legacy live (unparked) cards still discover work, but as explicit unknown, never as
 * certain running. Parked cards keep their legacy wait and blocked cards their legacy ask. */
const legacyCardsWithoutObservations = (row: Partial<Stamped> | undefined): number =>
  row && !Array.isArray(row.execution_observations)
    ? Math.max(0, (row.kanban_active ?? 0) - Math.max(0, row.kanban_parked ?? 0))
    : 0

function ownerSource(owner: ReturnType<typeof knownOwnerForSession>) {
  if (isSessionOwnerRoute(owner)) {
    return gatewayRouteState(owner.connectionId, owner.profile).key
  }

  if (owner) {
    return gatewayRouteState(null, String(owner)).key
  }

  return isActivePrimary() ? gatewayRouteState(null, activeGatewayProfileKey()).key : 'ambient'
}

export function rowExecution(
  row: Partial<Stamped> | undefined,
  nowSeconds: number = Date.now() / 1000
): null | RowExecution {
  const owner = row?.id ? knownOwnerForSession(row.id, [row as SessionInfo]) : undefined

  const source = ownerSource(owner)

  const roster = row?.id
    ? rosterRowEvidence($liveWorkRoster.get(), source, lineageAliases(row.id, $sessions.get()))
    : { records: [], missing: 0 }
  const list = [...(Array.isArray(row?.execution_observations) ? row.execution_observations : []), ...roster.records]
  let missing = roster.missing + legacyCardsWithoutObservations(row)

  if (row?.id) {
    const aliases = lineageAliases(row.id, $sessions.get())

    const matches = (runtime: string) =>
      aliases.includes($sessionStates.get()[runtime]?.storedSessionId ?? runtime) &&
      ownerSource(knownOwnerForSession(runtime)) === source

    for (const [kind, store] of [
      ['process', $backgroundStatusBySession.get()],
      ['subagent', $subagentsBySession.get()]
    ] as const) {
      const items = Object.entries(store).flatMap(([runtime, entries]) => (matches(runtime) ? entries : []))
      const active = items.filter(item => ('state' in item ? item.state : item.status) === 'running')
      list.push(...active.flatMap(item => item.executionObservations ?? []))

      // Do not count a missing event copy again when the source's roster/row has its evidence.
      const supplied = list.filter(
        record =>
          record && typeof record === 'object' && (record as { identity?: { kind?: string } }).identity?.kind === kind
      ).length

      if (roster.records.length === 0 && roster.missing === 0) {
        missing += Math.max(0, active.length - supplied)
      }
    }
  }

  if (list.length === 0 && missing === 0) {
    return null
  }

  // Capacity is diagnostic context, not another work item. The existing capacity producer stamps
  // kanban.dispatch; kanban.capacity is its explicit diagnostic identity. Never filter by state/reason:
  // malformed or unknown observations from any other source must remain visible.
  const work = list.filter(entry => {
    const source = entry !== null && typeof entry === 'object' ? (entry as Record<string, unknown>).source : undefined

    return source !== 'kanban.dispatch' && source !== 'kanban.capacity'
  })

  if (work.length === 0 && missing === 0) {
    return null
  }

  const summary = summarizeExecutionObservations(selectWorkObservations(work, nowSeconds), nowSeconds, 'sidebar')

  const { counts } = summary

  return {
    live: counts.running + counts.queued,
    needsUser: counts.needs_user,
    outstanding: summary.outstanding_human_requests,
    unknown: counts.unknown + missing,
    waiting: counts.waiting
  }
}

/** [daveey] `rowExecution`'s only read of `$sessionStates` is each runtime's stored id. That store is republished per
 *  streaming token; this key changes only when a runtime binds to (or leaves) a stored session. */
const $runtimeBindings = computed($sessionStates, states =>
  Object.entries(states)
    .map(([runtime, state]) => `${runtime}=${state?.storedSessionId ?? ''}`)
    .join('\n')
)

/** [daveey] What owner resolution (`knownOwnerForSession`) reads off the tiles: each tile's stored id, bound runtime
 *  and owner route/profile. Layout-only tile edits (dock, anchor, error) leave the key unchanged. */
const $tileOwnerBindings = computed($sessionTiles, tiles =>
  tiles
    .map(tile =>
      JSON.stringify([tile.storedSessionId, tile.runtimeId ?? '', tile.ownerProfile ?? '', tile.ownerRoute ?? null])
    )
    .join('\n')
)

/** [daveey] Every store `rowExecution` reads, including the owner route that picks its roster source: the tiles'
 *  owner bindings, the active profile and route selection (`$activeGatewayRoute`, `$gatewayActivationEpoch`), and
 *  the owner-lookup rows a runtime's owner falls back to. A hook that evaluates it for a render must subscribe to all
 *  of them (`useStoresSelector`), or its verdict lags until some unrelated store happens to notify.
 *  Not covered (module state, no store): the per-runtime event owner map (session-states.ts), remembered owner hints
 *  (session.ts) and the gateway registry's primary/secondary sockets; those changes repaint on the next 15 s tick. */
let rowExecutionStoreList: null | readonly ReadableAtom<unknown>[] = null

/** Built on first use, not at import: reading these bindings at module load throws in every suite that partially
 *  mocks `@/store/session` or `@/store/gateway` without them, though it never renders a dot. */
export const rowExecutionStores = (): readonly ReadableAtom<unknown>[] =>
  (rowExecutionStoreList ??= [
    $liveWorkRoster,
    $backgroundStatusBySession,
    $subagentsBySession,
    $runtimeBindings,
    $sessions,
    $tileOwnerBindings,
    $activeGatewayRoute,
    $gatewayActivationEpoch,
    $cronSessions,
    $messagingSessions,
    $projectTree,
    $unlistedSessionOwnerRows
  ])

/** Evidence that keeps a row off done: live/waiting/needs-you work, an outstanding ask, or unresolved uncertainty. */
export const executionHoldsRow = (e: null | RowExecution): boolean =>
  e !== null && e.live + e.waiting + e.needsUser + e.unknown + e.outstanding > 0

/** Stable signature for change detection (row signatures and the published per-row map). */
export const executionKey = (e: null | RowExecution): string =>
  e ? `${e.live}/${e.waiting}/${e.needsUser}/${e.unknown}/${e.outstanding}` : ''
