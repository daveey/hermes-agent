import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useState } from 'react'

import {
  $archivedSessions,
  $archivedSessionsHasMore,
  $archivedSessionsLoading,
  loadArchivedSessions,
  loadMoreArchivedSessions
} from '@/store/sidebar-archive'

/**
 * The sidebar's Archived view: its own rows and its own paging.
 *
 * Archived rows are excluded from the sessions query, so the view fetches its
 * own set (store/sidebar-archive): the first page when it opens, then one
 * 500-row page per load-more click. One per click because each page costs
 * more than the one before it, so a sparse page must not chain into the next.
 */
export function useArchivedView(active: boolean) {
  const sessions = useStore($archivedSessions)
  const hasMore = useStore($archivedSessionsHasMore)
  const loading = useStore($archivedSessionsLoading)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    if (active) {
      void loadArchivedSessions()
    }
  }, [active])

  const loadMore = useCallback(async () => {
    setLoadingMore(true)

    try {
      await loadMoreArchivedSessions()
    } finally {
      setLoadingMore(false)
    }
  }, [])

  return {
    /** Skeletons only while the view holds no row at all: a page with nothing
     *  for this profile is a loaded page and keeps its load-more row. */
    firstLoad: loading && sessions.length === 0,
    hasMore,
    loadMore,
    /** A click's own read, not a background refresh: that must not disable the row. */
    loadingMore,
    /** The Archived view's value while it is showing, the live list's otherwise. */
    pick: <T>(archived: T, live: T): T => (active ? archived : live),
    sessions
  }
}
