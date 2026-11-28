import {
  assembleSidebarSessionSlices,
  buildSidebarSessionSliceParams,
  tagSidebarSlicesOwner
} from './profile-session-routing'

/** Request-owned exact list bases; never retain a response across refreshes. */
export async function fetchSidebarPrimaryListBatch(
  params: URLSearchParams,
  deps: {
    fetch: (path: string) => Promise<unknown>
    merge: (params: URLSearchParams, base: unknown) => Promise<unknown>
    fallback: () => Promise<unknown>
    ownerTag: string | null
  }
): Promise<unknown> {
  const slices = buildSidebarSessionSliceParams(params)

  if (
    slices.recents.get('profile') !== 'all' ||
    Object.values(slices).some(sp => {
      const limit = Number(sp.get('limit'))

      return !Number.isInteger(limit) || limit < 1 || limit > 500
    })
  ) {
    return deps.fallback()
  }

  const query = new URLSearchParams(params)
  query.set('list_slices', 'true')
  let batch: Record<string, any>

  try {
    batch = (await deps.fetch(`/api/profiles/sessions/sidebar?${query}`)) as Record<string, any>

    if (
      batch?.list_slices_version !== 1 ||
      ['recents', 'cron', 'messaging'].some(key => {
        const slice = batch[key]

        return !Array.isArray(slice?.sessions) || !Number.isFinite(slice?.total) || !slice?.profile_totals
      })
    ) {
      return deps.fallback()
    }
  } catch {
    return deps.fallback()
  }

  batch = tagSidebarSlicesOwner(batch, deps.ownerTag) as Record<string, any>

  const [recents, cron, messaging] = await Promise.all(
    (['recents', 'cron', 'messaging'] as const).map(key => deps.merge(slices[key], batch[key]))
  )

  return assembleSidebarSessionSlices(recents, cron, messaging)
}
