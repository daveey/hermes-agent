import { describe, expect, it } from 'vitest'

import { REMOTE_UPDATE_PAUSED_PHRASE } from '../../../electron/remote-update-refusal'

import {
  isRemoteUpdatePausedBootError,
  REMOTE_UPDATE_POLL_MS,
  REMOTE_UPDATE_WAIT_MAX_MS,
  RemoteUpdateWait
} from './remote-update-wait'

describe('isRemoteUpdatePausedBootError', () => {
  it("recognises the refusal as it crosses IPC, and the gate's cached form", () => {
    expect(
      isRemoteUpdatePausedBootError(
        new Error(
          "Error invoking remote method 'hermes:connection:for': Error: Remote Hermes update process 1779396 is still running; SSH startup is paused."
        )
      )
    ).toBe(true)
    expect(isRemoteUpdatePausedBootError(new Error('Remote Hermes update in progress; SSH startup is paused.'))).toBe(
      true
    )
  })

  it('ignores other boot failures', () => {
    expect(isRemoteUpdatePausedBootError(new Error('Hermes backend did not become ready: timeout'))).toBe(false)
    expect(isRemoteUpdatePausedBootError(undefined)).toBe(false)
  })

  it('matches the shared phrase the electron producers word their refusal with', () => {
    expect(isRemoteUpdatePausedBootError(new Error(`Remote update; ${REMOTE_UPDATE_PAUSED_PHRASE}.`))).toBe(true)
  })
})

describe('RemoteUpdateWait', () => {
  it('polls until the cap, stays spent after it, and only reset starts a fresh wait', () => {
    let now = 1_000
    const wait = new RemoteUpdateWait(() => now)

    expect(wait.next()).toBe(REMOTE_UPDATE_POLL_MS)
    now += REMOTE_UPDATE_WAIT_MAX_MS - 1
    expect(wait.next()).toBe(REMOTE_UPDATE_POLL_MS)
    now += 1
    expect(wait.next()).toBeNull()
    now += 60_000
    expect(wait.next()).toBeNull()
    wait.reset()
    expect(wait.next()).toBe(REMOTE_UPDATE_POLL_MS)
  })
})
