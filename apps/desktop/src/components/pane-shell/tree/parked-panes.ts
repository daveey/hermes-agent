/**
 * PARKED PANES — registered contributions that are deliberately OUT of the
 * layout tree. A parked pane keeps its contribution, so a keep-alive body (a
 * live Browser guest) stays mounted — hidden and inert in its keep-alive host
 * — while adoption must not dock it back. The contribution's owner parks and
 * unparks it: a preview tab whose session is not on screen.
 *
 * An atom so a parked body can react to being parked (a Browser mutes its
 * page while its session is off screen).
 *
 * Memory-only: after a relaunch there is no live body left to keep.
 *
 * Parking is claimed per owner. Single-tab displacement and a retaining
 * pane-mirror can both park the same id; dropping one claim must not unpark
 * a pane another owner is still holding off screen.
 */

import { atom } from 'nanostores'

export const $parkedTreePanes = atom<ReadonlySet<string>>(new Set())

/** Single-tab displacement (see single-tab-state). */
export const TREE_PARK_SINGLE = 'single'

const claims = new Map<string, Set<string>>()
let publishing = false

$parkedTreePanes.listen(ids => {
  if (publishing) {
    return
  }

  for (const id of [...claims.keys()]) {
    if (!ids.has(id)) {
      claims.delete(id)
    }
  }

  for (const id of ids) {
    if (!claims.has(id)) {
      claims.set(id, new Set(['']))
    }
  }
})

function publish(): void {
  const next = new Set(claims.keys())
  const current = $parkedTreePanes.get()

  if (next.size === current.size && [...next].every(id => current.has(id))) {
    return
  }

  publishing = true
  $parkedTreePanes.set(next)
  publishing = false
}

export function isTreePaneParked(paneId: string): boolean {
  return $parkedTreePanes.get().has(paneId)
}

/** True when some owner other than `owner` still claims this pane. */
export function isTreePaneParkedByOthers(paneId: string, owner: string): boolean {
  const held = claims.get(paneId)

  if (!held || held.size === 0) {
    return false
  }

  return [...held].some(claim => claim !== owner)
}

export function setTreePaneParked(paneId: string, isParked: boolean, owner = ''): void {
  const held = claims.get(paneId) ?? new Set<string>()

  if (held.has(owner) === isParked) {
    return
  }

  const next = new Set(held)

  if (isParked) {
    next.add(owner)
  } else {
    next.delete(owner)
  }

  if (next.size) {
    claims.set(paneId, next)
  } else {
    claims.delete(paneId)
  }

  publish()
}
