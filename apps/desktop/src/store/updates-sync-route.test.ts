// @vitest-environment jsdom
// [daveey] On a host whose core updates hermes-sync applies, Update hands off to hermes-sync's Force-update request
// instead of running Desktop's own idle-only `hermes update`, which failed whenever a chat was busy (David-MBP
// 2026-11-20). Hosts hermes-sync does not manage keep the old path.
import { beforeEach, describe, expect, it, vi } from 'vitest'

const forceStateSpy = vi.fn()
const forceSpy = vi.fn()
const updateHermesSpy = vi.fn()

vi.mock('@/api/system', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  forceHermesUpdate: (...args: unknown[]) => forceSpy(...args),
  getForceUpdateState: (...args: unknown[]) => forceStateSpy(...args)
}))
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  checkHermesUpdate: vi.fn(),
  getActionStatus: vi.fn(),
  getHermesUpdateProgress: vi.fn().mockResolvedValue({ progress: null }),
  updateHermes: (...args: unknown[]) => updateHermesSpy(...args)
}))
const dismissSpy = vi.fn()
vi.mock('@/store/notifications', () => ({
  dismissNotification: (...args: unknown[]) => dismissSpy(...args),
  notify: vi.fn()
}))

const updates = await import('./updates')

const bridgeApply = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  bridgeApply.mockResolvedValue({ ok: true })
  window.hermesDesktop = { updates: { apply: bridgeApply, check: vi.fn(), onProgress: () => () => {} } } as never
  updates.resetUpdateApplyState()
  updates.$updateStatus.set({ supported: true, mechanism: 'posix-handoff' } as never)
})

const managed = { available: true, managed: true, pending: null, running_turns: 2 }

describe('Update on a host hermes-sync manages', () => {
  it('asks hermes-sync for the local host and never runs the detached updater', async () => {
    forceStateSpy.mockResolvedValue(managed)
    forceSpy.mockResolvedValue({ ok: true })

    const result = await updates.applyUpdates()

    expect(forceStateSpy).toHaveBeenCalledWith({ connectionId: 'local' })
    expect(forceSpy).toHaveBeenCalledWith({ connectionId: 'local' })
    expect(bridgeApply).not.toHaveBeenCalled()
    expect(result.ok).toBe(true)
    expect(updates.$updateApply.get()).toMatchObject({ applying: false, stage: 'synced' })
  })

  it('says hermes-sync has nothing pending instead of updating on its own', async () => {
    forceStateSpy.mockResolvedValue({ ...managed, available: false, message: 'hermes-sync applies Hermes updates' })

    const result = await updates.applyUpdates()

    expect(forceSpy).not.toHaveBeenCalled()
    expect(bridgeApply).not.toHaveBeenCalled()
    expect(result).toMatchObject({ ok: false, error: 'managed-by-hermes-sync' })
    expect(updates.$updateApply.get()).toMatchObject({
      stage: 'synced',
      message: 'hermes-sync applies Hermes updates'
    })
  })

  it('shows an error when the force request fails', async () => {
    forceStateSpy.mockResolvedValue(managed)
    forceSpy.mockRejectedValue(new Error('409 nothing pending'))

    const result = await updates.applyUpdates()

    expect(bridgeApply).not.toHaveBeenCalled()
    expect(result).toMatchObject({ ok: false, error: 'force-request-failed' })
    expect(updates.$updateApply.get()).toMatchObject({ stage: 'error', message: '409 nothing pending' })
  })

  it('says nothing is pending when the owed update lands between the read and the request (409)', async () => {
    forceStateSpy
      .mockResolvedValueOnce(managed)
      .mockResolvedValueOnce({ ...managed, available: false, message: 'hermes-sync applies Hermes updates' })
    forceSpy.mockRejectedValue(new Error('409 nothing pending'))

    const result = await updates.applyUpdates()

    expect(result).toMatchObject({ ok: false, error: 'managed-by-hermes-sync' })
    expect(updates.$updateApply.get()).toMatchObject({ stage: 'synced' })
  })

  it('leaves a packaged Desktop on its own bundle update', async () => {
    updates.$updateStatus.set({ supported: true, mechanism: 'electron-updater' } as never)
    forceStateSpy.mockResolvedValue(managed)

    await updates.applyUpdates()

    expect(forceStateSpy).not.toHaveBeenCalled()
    expect(forceSpy).not.toHaveBeenCalled()
    expect(bridgeApply).toHaveBeenCalledOnce()
  })

  it('hands the backend Update to hermes-sync without starting hermes update', async () => {
    let stageDuringRoute = ''
    forceStateSpy.mockImplementation(async () => {
      stageDuringRoute = updates.$backendUpdateApply.get().stage // preparing while the request is in flight

      return managed
    })
    forceSpy.mockResolvedValue({ ok: true })

    const result = await updates.applyBackendUpdate()

    expect(forceSpy).toHaveBeenCalledWith(undefined)
    expect(dismissSpy).toHaveBeenCalled() // the "update available" toast goes away on the hand-off too
    expect(stageDuringRoute).toBe('prepare')
    expect(updateHermesSpy).not.toHaveBeenCalled()
    expect(result.ok).toBe(true)
    expect(updates.$backendUpdateApply.get()).toMatchObject({ applying: false, stage: 'synced' })
    // The in-flight guard cleared: a second click runs again instead of joining a stale promise.
    await updates.applyBackendUpdate()
    expect(forceSpy).toHaveBeenCalledTimes(2)
  })

  it('routes the backend pill to the active backend host', async () => {
    forceStateSpy.mockResolvedValue(managed)
    forceSpy.mockResolvedValue({ ok: true })

    await updates.routeUpdateToHermesSync('backend')

    expect(forceStateSpy).toHaveBeenCalledWith(undefined)
    expect(forceSpy).toHaveBeenCalledWith(undefined)
    expect(updates.$backendUpdateApply.get()).toMatchObject({ stage: 'synced' })
  })
})

describe('Update on a host hermes-sync does not manage', () => {
  it('keeps the detached updater when the backend says unmanaged', async () => {
    forceStateSpy.mockResolvedValue({ available: false, pending: null, running_turns: 0 })

    await updates.applyUpdates()

    expect(forceSpy).not.toHaveBeenCalled()
    expect(bridgeApply).toHaveBeenCalledOnce()
  })

  it('keeps the detached updater when the force state cannot be read (older backend)', async () => {
    forceStateSpy.mockRejectedValue(new Error('404'))

    await updates.applyUpdates()

    expect(bridgeApply).toHaveBeenCalledOnce()
  })
})
