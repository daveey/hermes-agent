import { atom } from 'nanostores'

/** In-flight resume recovery, not messaging-gateway Settings' Restart action.
 * Keys contain the captured connection and stored session; counted tokens keep
 * concurrent weak/strong single-flight snapshots from clearing one another. */
export const $restartingSessionResumes = atom<ReadonlyMap<string, number>>(new Map())

export function holdRestartingSessionResume(key: string): () => void {
  const current = $restartingSessionResumes.get()
  $restartingSessionResumes.set(new Map(current).set(key, (current.get(key) ?? 0) + 1))
  let released = false

  return () => {
    if (released) { return }
    released = true
    const next = new Map($restartingSessionResumes.get())
    const count = (next.get(key) ?? 1) - 1

    if (count) { next.set(key, count) } else { next.delete(key) }
    $restartingSessionResumes.set(next)
  }
}

export function sessionResumeIsRestarting(sessionId: string | null | undefined, connectionId: string | null | undefined,
  pending = $restartingSessionResumes.get()): boolean {
  return Boolean(sessionId && pending.has(JSON.stringify([connectionId || '', sessionId])))
}
