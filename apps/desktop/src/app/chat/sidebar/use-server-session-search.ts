import { useCallback, useEffect, useState } from 'react'

import { searchSessions, type SessionSearchResult } from '@/hermes'
import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'

const SEARCH_DEBOUNCE_MS = 200

export interface ServerSessionSearch {
  serverMatches: SessionSearchResult[]
  searchPending: boolean
  searchFailed: boolean
  retrySearch: () => void
}

/**
 * Debounced full-text search across *all* sessions (not just the loaded page)
 * so old sessions stay findable. Hits come from the profile the sidebar shows:
 * an unscoped request searches the primary backend's launch profile, whatever
 * profile is on screen. "All profiles" asks the primary as before.
 */
export function useServerSessionSearch(query: string, profileScope: string): ServerSessionSearch {
  const [serverMatches, setServerMatches] = useState<SessionSearchResult[]>([])
  const [searchPending, setSearchPending] = useState(false)
  const [searchFailed, setSearchFailed] = useState(false)
  const [retryAttempt, setRetryAttempt] = useState(0)
  const retrySearch = useCallback(() => setRetryAttempt(attempt => attempt + 1), [])
  const profile = profileScope === ALL_PROFILES ? null : normalizeProfileKey(profileScope)

  useEffect(() => {
    setSearchFailed(false)
    setServerMatches([])

    if (!query) {
      setSearchPending(false)

      return
    }

    let cancelled = false

    setSearchPending(true)

    const id = window.setTimeout(() => {
      void searchSessions(query, profile)
        .then(res => {
          if (!cancelled) {
            setServerMatches(res.results)
          }
        })
        .catch(() => {
          if (!cancelled) {
            setSearchFailed(true)
            setServerMatches([])
          }
        })
        .finally(() => {
          if (!cancelled) {
            setSearchPending(false)
          }
        })
    }, SEARCH_DEBOUNCE_MS)

    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [query, profile, retryAttempt])

  return { searchPending, serverMatches, searchFailed, retrySearch }
}
