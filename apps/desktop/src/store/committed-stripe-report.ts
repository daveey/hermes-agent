/**
 * [daveey] Committed-paint report: every mounted surface (row stripe, pane tab, status dot) tells the session's own
 * gateway which status it actually painted, qualified by the envelope's identity / revision / generation /
 * coverage. Not red-only: done, waiting, attention, none and unknown are reported too, so the backend can compare
 * what was painted with what it published at that exact revision. A surface that painted this window's own live
 * turn or a ⌘B park over the envelope says so (`local_override`): that is neither agreement nor drift. A row whose
 * outcome carries no envelope (an older backend) reports nothing.
 *
 * Custody is per renderer client, session, surface and mount lifetime. Simultaneous mounts report independently;
 * unmount is terminal only for its own lifetime. A monotonic event sequence fences delayed same-revision sends.
 * Within a lifetime/qualified owner, canonical reports older than the highest revision seen are dropped, and a
 * queued retry is replaced by a newer commit, so a late failure can never re-send (or acknowledge) a stale paint.
 * Entries are capped at 512 (including tombstones); eviction cancels retries. Backend diagnostics are likewise
 * bounded observations, never status authority or proof that an unreported/crashed client is still mounted.
 * Delivery is bounded: a failed send is retried on a short fixed backoff up to `COMMITTED_STRIPE_MAX_ATTEMPTS`
 * sends in total, and `flushCommittedStripeReports` (gateway reconnect) re-sends whatever is still pending once.
 * No poller: nothing sends without a commit or a reconnect.
 */
import { randomUuid } from '@hermes/shared'
import { useLayoutEffect, useRef } from 'react'

import { canonicalOutcomeStatus } from '@/lib/session-outcome'
import type { SessionInfo } from '@/types/hermes'
import type { SessionOutcome, SessionStatusEnvelope } from '@/types/session-outcome'

export type CommittedStripeSurface = 'dot' | 'row' | 'tab'

/** The qualified payload; `stripe` is `none` for a committed blank, `attention` for an unknown envelope. */
export interface CommittedStripeReport {
  observation_client: string
  mounted: boolean
  observation_lifetime: number
  observation_order: number
  coverage?: NonNullable<SessionStatusEnvelope['coverage']>
  generation?: number
  identity?: NonNullable<SessionStatusEnvelope['identity']>
  /** Set when the surface painted something it knows locally instead of this revision's stripe. */
  local_override?: string
  reason_codes: string[]
  revision: null | number
  status: 'canonical' | 'unknown'
  status_version: 1
  stripe: string
  surface: CommittedStripeSurface
}

export type CommittedStripeRow = Pick<SessionInfo, 'connection_id' | 'id' | 'profile'>
export type CommittedStripeReporter = (
  row: CommittedStripeRow,
  report: CommittedStripeReport
) => Promise<unknown> | void

export const COMMITTED_STRIPE_MAX_ATTEMPTS = 4
export const COMMITTED_STRIPE_MAX_ENTRIES = 512
// A renderer boot is a client. Its sequence is monotonic, never compared with another client's clock.
const observationClient = randomUuid()
let clock = 0
const tick = (): number => ++clock
const RETRY_DELAYS_MS = [1_000, 5_000, 15_000]

interface Pending {
  inFlight?: boolean
  attempts: number
  report: CommittedStripeReport
  row: CommittedStripeRow
}

interface Entry {
  lifetime: number
  unmounted: boolean
  acked: null | string
  highest: number
  /** Qualified owner of `highest` / `acked`; a different owner resets the revision fence (its revisions restart). */
  owner: string
  retiredOwners: Set<string>
  pending: null | Pending
  timer: null | ReturnType<typeof setTimeout>
}

const entries = new Map<string, Entry>()
let reporter: CommittedStripeReporter | null = null

export const setCommittedStripeReporter = (next: CommittedStripeReporter | null): void => {
  reporter = next
}

/** Test hook: forget all fences, acknowledgements and timers. */
export const resetCommittedStripeReports = (): void => {
  for (const entry of entries.values()) {
    if (entry.timer) {
      clearTimeout(entry.timer)
    }
  }

  entries.clear()
}

const ownerKey = (report: CommittedStripeReport): string =>
  report.identity
    ? [report.identity.gateway, report.identity.profile, report.identity.session_id, report.identity.owner_epoch].join(
        '\u0001'
      )
    : ''

const signature = (report: CommittedStripeReport): string =>
  [
    report.status,
    ownerKey(report),
    report.revision ?? '',
    report.generation ?? '',
    report.stripe,
    report.local_override ?? '',
    report.mounted,
    report.observation_lifetime
  ].join('\u0000')

/** `painted` is the stripe the surface ACTUALLY committed (null = nothing painted), not the one the envelope intends. */
const buildReport = (
  outcome: null | SessionOutcome | undefined,
  surface: CommittedStripeSurface,
  painted: null | string,
  override: null | string
): null | Omit<CommittedStripeReport, 'mounted' | 'observation_client' | 'observation_lifetime' | 'observation_order'> => {
  const status = canonicalOutcomeStatus(outcome)
  const envelope = outcome?.status
  const stripe = painted ?? 'none'
  const local = override ? { local_override: override } : {}

  if (status.status === 'unknown' || !envelope) {
    return { ...local, reason_codes: [], revision: null, status: 'unknown', status_version: 1, stripe, surface }
  }

  return {
    ...local,
    coverage: envelope.coverage,
    generation: envelope.generation,
    identity: envelope.identity,
    reason_codes: envelope.presentation?.reason_codes ?? [],
    revision: status.revision,
    status: 'canonical',
    status_version: 1,
    stripe,
    surface
  }
}

const attempt = (key: string): Promise<void> => {
  const entry = entries.get(key)
  const pending = entry?.pending

  if (!entry || !pending || !reporter || pending.inFlight || pending.attempts >= COMMITTED_STRIPE_MAX_ATTEMPTS) {
    return Promise.resolve()
  }

  if (entry.timer) {
    clearTimeout(entry.timer)
    entry.timer = null
  }

  pending.attempts += 1
  pending.inFlight = true

  const fail = () => {
    pending.inFlight = false

    if (entries.get(key) !== entry || entry.pending !== pending) {
      return
    }

    if (pending.attempts >= COMMITTED_STRIPE_MAX_ATTEMPTS) {
      entry.pending = null

      return
    }

    entry.timer = setTimeout(() => void attempt(key), RETRY_DELAYS_MS[pending.attempts - 1] ?? 15_000)
  }

  const succeed = () => {
    if (entries.get(key) === entry && entry.pending === pending) {
      entry.acked = signature(pending.report)
      entry.pending = null
    }
  }

  try {
    // A transport that resolves `{ ok: false }` (or throws / rejects) did not deliver: only a delivered report acks.
    return Promise.resolve(reporter(pending.row, pending.report)).then(result => {
      if (result && typeof result === 'object' && (result as { ok?: unknown }).ok === false) {
        fail()
      } else {
        succeed()
      }
    }, fail)
  } catch {
    fail()

    return Promise.resolve()
  }
}

const pruneEntries = (): void => {
  while (entries.size > COMMITTED_STRIPE_MAX_ENTRIES) {
    const oldest = entries.keys().next().value!
    const removed = entries.get(oldest)!

    if (removed.timer) {clearTimeout(removed.timer)}
    entries.delete(oldest)
  }
}

const acceptLifetime = (entry: Entry, report: CommittedStripeReport): boolean => {
  if (report.observation_lifetime < entry.lifetime ||
      (report.observation_lifetime === entry.lifetime && entry.unmounted && report.mounted)) {return false}

  if (report.observation_lifetime > entry.lifetime) {entry.unmounted = false}
  entry.lifetime = report.observation_lifetime

  if (!report.mounted) {entry.unmounted = true}

  return true
}

/** Report what a mounted surface just committed for `row`. Call from a commit effect, never from render. */
export const reportCommittedStripe = (
  row: CommittedStripeRow,
  outcome: null | SessionOutcome | undefined,
  surface: CommittedStripeSurface,
  painted: null | string,
  lifetime?: number,
  mounted = true,
  override: null | string = null
): void => {
  const built = buildReport(outcome, surface, painted, override)

  if (!built) {
    return
  }

  const report: CommittedStripeReport = {
    ...built,
    mounted,
    observation_client: observationClient,
    observation_lifetime: lifetime ?? 0,
    observation_order: tick()
  }

  // Every simultaneous mount has independent custody: one mount's cleanup cannot hide another one's paint.
  const key = `${row.connection_id ?? ''}\u0000${row.profile ?? ''}\u0000${row.id}\u0000${surface}\u0000${report.observation_lifetime}`

  const entry = entries.get(key) ?? {
    lifetime: -1,
    unmounted: false,
    acked: null,
    highest: -1,
    owner: '',
    retiredOwners: new Set<string>(),
    pending: null,
    timer: null
  }

  entries.set(key, entry)
  pruneEntries()

  if (!acceptLifetime(entry, report)) {return}

  const owner = ownerKey(report)

  // Unmount is terminal for this lifetime even when its final envelope was stale or a retired owner.
  if (mounted && report.revision !== null) {
    if (owner !== entry.owner) {
      // New qualified owner (owner_epoch / gateway change): its revisions restart, so the old fence must not drop it.
      if (entry.retiredOwners.has(owner)) {
        return
      }

      if (entry.owner) {
        // Fail closed instead of forgetting an owner and permitting ABA after churn.
        if (entry.retiredOwners.size >= 32) {return}
        entry.retiredOwners.add(entry.owner)
      }

      entry.owner = owner
      entry.highest = -1
      entry.acked = null
    } else if (report.revision < entry.highest) {
      return
    }

    entry.highest = report.revision
  }

  if ((!entry.pending && entry.acked === signature(report)) ||
      (entry.pending && signature(entry.pending.report) === signature(report))) {
    return
  }

  // A superseded pending report's retry timer must not fire late (old owner / older revision).
  if (entry.timer) {
    clearTimeout(entry.timer)
    entry.timer = null
  }

  entry.pending = { attempts: 0, report, row }
  void attempt(key)
}

/** Gateway reconnected: send each still-pending report once more (bounded by the attempt cap). */
export const flushCommittedStripeReports = async (): Promise<void> => {
  await Promise.all([...entries.keys()].map(attempt))
}

/** One lifetime per actual mount, not per revision effect. Cleanup reports the last committed envelope. */
export function useCommittedStripe(
  row: CommittedStripeRow,
  outcome: null | SessionOutcome | undefined,
  surface: CommittedStripeSurface,
  painted: null | string,
  enabled = true,
  override: null | string = null
): void {
  const current = useRef({ row, outcome, painted, override })
  const lifetime = useRef(0)
  const { connection_id, id, profile } = row
  // This is custody of the last COMMITTED DOM paint, not a mirror of a live atom.
  // Layout cleanup must publish that old paint before a new identity can commit.
  useLayoutEffect(() => {
    if (!enabled) {return}
    lifetime.current = tick()

    return () => {
      const last = current.current
      reportCommittedStripe(last.row, last.outcome, surface, last.painted, lifetime.current, false, last.override)
    }
  }, [connection_id, id, profile, surface, enabled])
  useLayoutEffect(() => {
    const committedRow = { connection_id, id, profile }
    current.current = { row: committedRow, outcome, painted, override }

    if (enabled) {
      reportCommittedStripe(committedRow, outcome, surface, painted, lifetime.current, true, override)
    }
  }, [connection_id, id, profile, surface, outcome, painted, enabled, override])
}
