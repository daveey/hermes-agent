/** [daveey] Maps host update progress for post-swap version refreshes. Hub owns update UI. */
export type BackgroundUpdateState = 'applying' | 'batching' | 'blocked' | 'draining'

/** Wire shape of `GET /api/hermes/update/progress` (absent on older backends: the route 404s). */
export interface UpdateProgressResponse {
  progress: null | {
    state: string
    due_at: number
    state_since: number
    elapsed_s: number
    state_elapsed_s?: number
    cap_s: null | number
    waiting_on: string[]
  }
}

export interface BackgroundUpdateProgress {
  state: BackgroundUpdateState
  elapsedS: number
  stateElapsedS: number
  capS: null | number
  waitingOn: string[]
  fetchedAt: number
}

const STATES: readonly BackgroundUpdateState[] = ['applying', 'batching', 'blocked', 'draining']

export function mapUpdateProgress(
  response: null | undefined | UpdateProgressResponse,
  fetchedAt: number
): BackgroundUpdateProgress | null {
  const raw = response?.progress

  if (!raw || !STATES.includes(raw.state as BackgroundUpdateState) || !Number.isFinite(raw.elapsed_s)) {
    return null
  }

  return {
    state: raw.state as BackgroundUpdateState,
    elapsedS: raw.elapsed_s,
    stateElapsedS: Number.isFinite(raw.state_elapsed_s)
      ? Math.max(0, raw.state_elapsed_s as number)
      : Math.max(0, raw.elapsed_s),
    capS: typeof raw.cap_s === 'number' && raw.cap_s > 0 ? raw.cap_s : null,
    waitingOn: Array.isArray(raw.waiting_on) ? raw.waiting_on.filter(item => typeof item === 'string') : [],
    fetchedAt
  }
}
