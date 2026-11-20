// @vitest-environment jsdom
// [daveey] The version pills poll each host's own background core update (hermes-sync): the client pill reads
// this machine (pinned local even while a remote backend is primary), the backend pill the remote host.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

const progressSpy = vi.fn()
const checkHermesUpdateSpy = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  checkHermesUpdate: (...args: unknown[]) => checkHermesUpdateSpy(...args),
  getActionStatus: vi.fn(),
  getHermesUpdateProgress: (...args: unknown[]) => progressSpy(...args),
  updateHermes: vi.fn()
}))
vi.mock('@/store/notifications', () => ({ dismissNotification: vi.fn(), notify: vi.fn() }))

const updates = await import('./updates')
const { setConnection } = await import('./session')

const remote: HermesConnection = {
  baseUrl: 'https://hub.example',
  isFullscreen: false,
  logs: [],
  mode: 'remote',
  nativeOverlayWidth: 0,
  remoteKind: 'ssh',
  token: 't',
  windowButtonPosition: null,
  wsUrl: 'wss://hub.example'
}

const draining = {
  progress: {
    state: 'draining',
    due_at: 1,
    state_since: 1,
    elapsed_s: 40,
    state_elapsed_s: 40,
    cap_s: 60,
    waiting_on: ['default:s']
  }
}

const applying = { progress: { ...draining.progress, state: 'applying' } }
const clientCheck = vi.fn().mockResolvedValue({ supported: true, behind: 0 })

beforeEach(() => {
  vi.clearAllMocks()
  checkHermesUpdateSpy.mockResolvedValue({ behind: 0, can_apply: true, current_version: '1', update_available: false })
  window.hermesDesktop = { updates: { apply: vi.fn(), check: clientCheck, onProgress: () => () => {} } } as never
  updates.$clientUpdateProgress.set(null)
  updates.$backendUpdateProgress.set(null)
})

afterEach(async () => {
  // Module state outlives a test: stop the poller, unfreeze the clock, and let one answered round clear any
  // kept/dropped state, so a failure here never cascades into the next test.
  updates.stopUpdatePoller()
  vi.restoreAllMocks()
  progressSpy.mockReset()
  progressSpy.mockResolvedValue({ progress: null })
  setConnection(remote)
  await updates.pollUpdateProgress()
  setConnection(null as never)
})

describe('pollUpdateProgress', () => {
  it('pins the client pill to this machine and reads the remote host for the backend pill', async () => {
    setConnection(remote)
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) =>
      scope?.connectionId === 'local' ? { progress: null } : draining
    )

    await updates.pollUpdateProgress()

    expect(progressSpy).toHaveBeenCalledWith({ connectionId: 'local' })
    expect(progressSpy).toHaveBeenCalledWith(undefined)
    expect(updates.$clientUpdateProgress.get()).toBeNull()
    expect(updates.$backendUpdateProgress.get()).toMatchObject({
      state: 'draining',
      elapsedS: 40,
      capS: 60,
      waitingOn: ['default:s']
    })
  })

  it('asks no remote host while local', async () => {
    setConnection({ ...remote, mode: 'local' })
    updates.$backendUpdateProgress.set({
      state: 'applying',
      elapsedS: 1,
      stateElapsedS: 1,
      capS: null,
      waitingOn: [],
      fetchedAt: 0
    })
    progressSpy.mockResolvedValue({ progress: null })

    await updates.pollUpdateProgress()

    expect(progressSpy).toHaveBeenCalledTimes(1)
    expect(progressSpy).toHaveBeenCalledWith({ connectionId: 'local' })
    expect(updates.$backendUpdateProgress.get()).toBeNull()
  })

  it('an unreachable host (restarting for this update) keeps the pill as it was and refreshes nothing', async () => {
    setConnection(remote)
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => (scope ? { progress: null } : applying))
    await updates.pollUpdateProgress()

    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => {
      if (scope) {
        return { progress: null }
      }

      throw new Error('backend restarting')
    })
    await updates.pollUpdateProgress()

    expect(updates.$backendUpdateProgress.get()).toMatchObject({ state: 'applying' })
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()
  })

  it('drops a kept state once the host has been silent past the stale bound, and refreshes when it answers', async () => {
    // A restart (or a Mac asleep) longer than the bound: the busy state is dropped without a refresh, but the
    // update it showed was never seen to finish, so the host's next "nothing in progress" still refreshes.
    setConnection(remote)
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => (scope ? { progress: null } : applying))
    await updates.pollUpdateProgress()

    pastStaleBound()
    progressSpy.mockImplementation(failRemote)
    await updates.pollUpdateProgress()
    vi.restoreAllMocks()

    expect(updates.$backendUpdateProgress.get()).toBeNull()
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()

    progressSpy.mockImplementation(async () => ({ progress: null }))
    await updates.pollUpdateProgress()

    await vi.waitFor(() => expect(checkHermesUpdateSpy).toHaveBeenCalledWith(true))
    expect(checkHermesUpdateSpy).toHaveBeenCalledTimes(1)

    // Once answered, the next quiet answer is just quiet: no second refresh.
    await updates.pollUpdateProgress()
    expect(checkHermesUpdateSpy).toHaveBeenCalledTimes(1)
  })

  const failRemote = async (scope?: { connectionId?: string }) => {
    if (scope) {
      return { progress: null }
    }

    throw new Error('gone')
  }

  const pastStaleBound = () => vi.spyOn(Date, 'now').mockReturnValue(Date.now() + updates.UPDATE_PROGRESS_STALE_MS + 1)

  it('a dropped update on one remote never refreshes the next remote', async () => {
    setConnection(remote)
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => (scope ? { progress: null } : applying))
    updates.startUpdatePoller() // running, as in the app: its own poll and connection subscription are live
    await vi.waitFor(() => expect(updates.$backendUpdateProgress.get()).toMatchObject({ state: 'applying' }))

    pastStaleBound()
    progressSpy.mockImplementation(failRemote)
    await updates.pollUpdateProgress()
    expect(updates.$backendUpdateProgress.get()).toBeNull()
    vi.restoreAllMocks()

    // The new host answers with a quiet, non-busy state the pill shows: seeing it there proves the switch's own
    // poll has landed, and a non-busy answer is exactly what a leftover dropped flag would turn into a refresh.
    const batching = { progress: { ...draining.progress, state: 'batching', elapsed_s: -600 } }

    progressSpy.mockReset()
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => (scope ? { progress: null } : batching))
    setConnection({ ...remote, baseUrl: 'https://other.example', wsUrl: 'wss://other.example' })
    await vi.waitFor(() => expect(updates.$backendUpdateProgress.get()).toMatchObject({ state: 'batching' }))

    expect(checkHermesUpdateSpy).not.toHaveBeenCalledWith(true)
  })

  it('a dropped state that was only batched never triggers the refresh', async () => {
    const batching = { progress: { ...draining.progress, state: 'batching', elapsed_s: -600 } }

    setConnection(remote)
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => (scope ? { progress: null } : batching))
    await updates.pollUpdateProgress()
    expect(updates.$backendUpdateProgress.get()).toMatchObject({ state: 'batching' })

    pastStaleBound()
    progressSpy.mockImplementation(failRemote)
    await updates.pollUpdateProgress()
    vi.restoreAllMocks()
    expect(updates.$backendUpdateProgress.get()).toBeNull()

    progressSpy.mockImplementation(async () => ({ progress: null }))
    await updates.pollUpdateProgress()
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()
  })

  it('never stacks a second request on a host that has not answered yet', async () => {
    setConnection(remote)

    let release: (value: unknown) => void = () => {}
    progressSpy.mockImplementation((scope?: { connectionId?: string }) =>
      scope ? Promise.resolve({ progress: null }) : new Promise(r => (release = r))
    )

    const first = updates.pollUpdateProgress()

    try {
      await updates.pollUpdateProgress()
    } finally {
      // Always answer the held request, even if an assertion above throws: a request left in flight would keep
      // this host's poll blocked through afterEach and fail the tests after this one.
      release(draining)
      await first
    }

    expect(progressSpy.mock.calls.filter(([scope]) => scope === undefined)).toHaveLength(1)
    expect(updates.$backendUpdateProgress.get()).toMatchObject({ state: 'draining' })
  })

  it('refreshes the backend version check once its update finishes, not while it runs', async () => {
    setConnection(remote)
    progressSpy.mockImplementation(async (scope?: { connectionId?: string }) => (scope ? { progress: null } : applying))
    await updates.pollUpdateProgress()
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()

    progressSpy.mockImplementation(async () => ({ progress: null }))
    await updates.pollUpdateProgress()
    await vi.waitFor(() => expect(checkHermesUpdateSpy).toHaveBeenCalledWith(true))
    expect(updates.$backendUpdateProgress.get()).toBeNull()
  })
})
