import { backendScopeKey } from '@hermes/shared'
import { atom, computed } from 'nanostores'

import { $apiRequestScope } from '@/api/client'
import { listAllProfileSessions } from '@/api/sessions'
import type { SessionInfo } from '@/types/hermes'

import { $sidebarShowArchived, $sidebarShowAutomated } from './layout'
import { $sessions } from './session'

// The backend's `/api/profiles/sessions` page cap (`le=500`). The Electron
// merge path (remote profiles, registry gateways) answers every page by
// re-reading the prefix through offset + limit, so fewer, larger pages keep a
// deep archive from re-reading that prefix once per 200 rows.
const ARCHIVED_FETCH_PAGE_SIZE = 500

/** One page-at-a-time cursor over `archived=only`. `next()` reads one more
 *  page (a call made while a read is running joins that read); `sessions` is
 *  everything read so far and `hasMore` says whether the server still holds
 *  archived rows past the cursor. */
export interface ArchivedPager {
  hasMore: boolean
  next: () => Promise<SessionInfo[]>
  sessions: SessionInfo[]
}

/**
 * A cursor over `archived=only`, one page per `next()`.
 *
 * The Archived view shows a page and reads the next one on request. It never
 * reads the whole archive up front: an archive can run to tens of thousands
 * of rows (bot profiles auto-archive every run), the Electron merge re-reads
 * the prefix for each page, and a view that waits for the last page shows
 * nothing at all until then.
 *
 * The offset advances by the page size only — never by the number of rows
 * received. The list endpoints deliberately back-fill pinned conversations
 * past their LIMIT (and the pin back-fill ignores the archived filter, so a
 * non-archived pin rides along on an archived page), which means a page can
 * return more rows than `limit`. Using the row count as the next offset would
 * skip archived rows that the back-fill displaced; the page size is the only
 * cursor the server's pagination contract guarantees.
 *
 * Back-filled pins can repeat across pages (a pinned row past the window is
 * re-appended by every page whose window covers it), so rows dedupe by owner
 * and id on the way in. `hasMore` turns false once `page.total` (which counts
 * only archived rows) distinct archived rows have arrived. Older backends can
 * let a live pin displace an archived row from a page; cursor >= total would
 * leave that final row unread. An empty page also ends it. Live pins can ride
 * along even past the end, so once the cursor has covered `total`, a page
 * with no new archived row ends it too: a row archived or reordered mid-load
 * can leave the distinct count short for good.
 *
 * `includeAutomated: false` asks for pages without the rows each store marks
 * automated. Bot profiles hold most of a deep archive, so leaving them in
 * buries the user's own chats pages down. That drops every automated row,
 * including the pinned or needs-attention ones the live sidebar would keep:
 * those are listed again with "Show automated & test sessions" on.
 */
export function createArchivedPager(includeAutomated: boolean): ArchivedPager {
  const filter = includeAutomated ? {} : ({ automated: 'exclude' } as const)
  const seen = new Set<string>()
  let archivedCount = 0
  let offset = 0
  let reading: null | Promise<SessionInfo[]> = null

  const pager: ArchivedPager = {
    hasMore: true,
    sessions: [],
    next() {
      reading ??= readPage().finally(() => (reading = null))

      return reading
    }
  }

  async function readPage(): Promise<SessionInfo[]> {
    if (!pager.hasMore) {
      return pager.sessions
    }

    const page = await listAllProfileSessions(ARCHIVED_FETCH_PAGE_SIZE, 0, 'only', 'recent', 'all', filter, offset)
    const archivedBefore = archivedCount
    const sessions = [...pager.sessions]

    for (const session of page.sessions) {
      const identity = `${session.connection_id || ''}\0${session.profile || 'default'}\0${session.id}`

      if (!seen.has(identity)) {
        seen.add(identity)
        sessions.push(session)

        if (session.archived) {
          archivedCount += 1
        }
      }
    }

    offset += ARCHIVED_FETCH_PAGE_SIZE
    pager.sessions = sessions
    pager.hasMore = !(
      page.sessions.length === 0 ||
      archivedCount >= page.total ||
      (offset >= page.total && archivedCount === archivedBefore)
    )

    return sessions
  }

  return pager
}

export const $archivedSessions = atom<SessionInfo[]>([])
export const $archivedSessionsLoading = atom(false)
/** Whether the server holds archived rows past the pages loaded so far. */
export const $archivedSessionsHasMore = atom(false)

// Each load takes a generation; only the newest may publish or clear the
// flag. The scope key pins it to the connection/profile it was issued on, so
// a switch mid-load starts a fresh fetch and strands the previous one.
let archiveGeneration = 0
let loadingScope: null | string = null
let running: null | Promise<void> = null
// The cursor behind the published rows, and how many pages it has read.
let publishedPager: ArchivedPager | null = null
let publishedPages = 0
// How many pages the view should hold: one, plus one per load-more. A running
// load reads this as it goes, so a load-more asked for mid-refresh is read by
// that refresh instead of being dropped.
let wantedPages = 1

function archiveScopeKey(): string {
  const { connectionId, profile } = $apiRequestScope.get()

  return backendScopeKey(connectionId, profile)
}

function publish(pager: ArchivedPager, pages: number): void {
  publishedPager = pager
  publishedPages = pages
  $archivedSessions.set(pager.sessions)
  $archivedSessionsHasMore.set(pager.hasMore)
}

/** Whether the view still shows exactly what the published cursor read. An
 *  archive, unarchive or delete edits the list in place; the server's offsets
 *  then no longer line up with the cursor's, and its next page would skip a
 *  row per removal and bring the removed rows back. */
function cursorMatchesView(): boolean {
  return publishedPager !== null && $archivedSessions.get() === publishedPager.sessions
}

async function runArchiveLoad(extend: boolean): Promise<void> {
  const scope = archiveScopeKey()
  const generation = ++archiveGeneration
  const current = () => generation === archiveGeneration && archiveScopeKey() === scope
  loadingScope = scope

  $archivedSessionsLoading.set(true)

  try {
    // One more page can come off the published cursor; anything else starts over.
    let pager = extend && cursorMatchesView() ? publishedPager : null
    let read = pager ? publishedPages : 0

    for (let pass = 0; pass < 2 && current(); pass += 1) {
      const continued = pager !== null
      const firstLoad = $archivedSessions.get().length === 0
      pager ??= createArchivedPager($sidebarShowAutomated.get())

      while (read < wantedPages && pager.hasMore && current()) {
        const shown = $archivedSessions.get()

        await pager.next()
        read += 1

        if (continued && $archivedSessions.get() !== shown) {
          // The list was edited while the cursor read on: start over.
          break
        }

        if (current() && (firstLoad || read >= wantedPages || !pager.hasMore)) {
          publish(pager, read)
        }
      }

      if (!continued || cursorMatchesView()) {
        break
      }

      pager = null
      read = 0
    }
  } catch {
    // A background refresh must not turn a usable Archived view into an empty
    // one when the backend is temporarily unavailable. Keep the last good set;
    // a failed load-more stays offered and the next click retries it.
  } finally {
    if (generation === archiveGeneration) {
      wantedPages = Math.max(1, publishedPages)
      $archivedSessionsLoading.set(false)
    }
  }
}

function startArchiveLoad(extend: boolean): Promise<void> {
  const load = runArchiveLoad(extend).finally(() => {
    if (running === load) {
      running = null
    }
  })

  running = load

  return load
}

/** Load the Archived view: its first page, or on a refresh as many pages as
 *  the user has paged in, so rows they loaded do not vanish. */
export async function loadArchivedSessions(): Promise<void> {
  if ($archivedSessionsLoading.get() && loadingScope === archiveScopeKey()) {
    return
  }

  await startArchiveLoad(false)
}

/** Read one more page into the Archived view; resolves when it is in. A
 *  no-op when nothing is left. Asked for while a load is running, that load
 *  reads the extra page. A failure leaves the loaded rows as they are. */
export async function loadMoreArchivedSessions(): Promise<void> {
  if (!$archivedSessionsHasMore.get()) {
    return
  }

  wantedPages = publishedPages + 1

  if ($archivedSessionsLoading.get() && loadingScope === archiveScopeKey() && running) {
    await running

    return
  }

  await startArchiveLoad(true)
}

// Rows from another connection/profile must not linger (or be acted on), and
// nothing else reloads on a switch: the sidebar effect watches only the
// Archived toggle and background sync waits for a backend sessions.changed.
// Clear on every real scope change; reload (superseding a load stranded on
// the old scope) while Archived is showing. The keep-last-good fallback in
// loadArchivedSessions is therefore per scope.
let observedScope = archiveScopeKey()

$apiRequestScope.listen(() => {
  const scope = archiveScopeKey()

  if (scope === observedScope) {
    return
  }

  observedScope = scope
  restartArchivedView()
})

// The loaded pages were asked for with or without automated rows; the other
// answer is a different list, not a filter over this one.
$sidebarShowAutomated.listen(restartArchivedView)

function restartArchivedView(): void {
  // Strand whatever is reading the old scope: it may neither publish nor
  // count its pages against the new one.
  archiveGeneration += 1
  publishedPager = null
  publishedPages = 0
  wantedPages = 1
  $archivedSessions.set([])
  $archivedSessionsHasMore.set(false)
  $archivedSessionsLoading.set(false)

  if ($sidebarShowArchived.get()) {
    void loadArchivedSessions()
  }
}

/** Spend on a session — provider-reported price when we have one, our own
 *  estimate otherwise. */
export const sessionCostUsd = (session: SessionInfo): number =>
  session.actual_cost_usd || session.estimated_cost_usd || 0

/** Whether ANY loaded session reports spend. Subscription auth never quotes a
 *  price, so for those users a cost sort would rank a list of zeroes — the
 *  menu hides the option instead of offering a dead one. */
export const $sessionsHaveCost = computed($sessions, sessions => sessions.some(session => sessionCostUsd(session) > 0))
