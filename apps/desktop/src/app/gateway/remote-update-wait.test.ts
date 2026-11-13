import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  isRemoteUpdatePausedBootError,
  REMOTE_UPDATE_POLL_MS,
  REMOTE_UPDATE_WAIT_MAX_MS,
  RemoteUpdateWait
} from './remote-update-wait'

const electronSource = (name: string) => readFileSync(join(process.cwd(), 'electron', name), 'utf8')

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

  // The renderer only sees text, so every producer of the refusal must keep the shared phrase.
  it.each(['remote-lifecycle.ts', 'windows-remote-lifecycle.ts', 'remote-update-pause.ts'])(
    '%s still words its update refusal with the phrase the renderer matches',
    name => {
      const messages = electronSource(name).match(/['`][^'`]*SSH startup is paused[^'`]*['`]/g) ?? []

      expect(messages.length).toBeGreaterThan(0)

      for (const message of messages) {
        expect(isRemoteUpdatePausedBootError(message)).toBe(true)
      }
    }
  )
})

describe('RemoteUpdateWait', () => {
  it('polls until the cap, then gives up; reset starts a fresh wait', () => {
    let now = 1_000
    const wait = new RemoteUpdateWait(() => now)

    expect(wait.waiting).toBe(false)
    expect(wait.next()).toBe(REMOTE_UPDATE_POLL_MS)
    expect(wait.waiting).toBe(true)
    now += REMOTE_UPDATE_WAIT_MAX_MS - 1
    expect(wait.next()).toBe(REMOTE_UPDATE_POLL_MS)
    now += 1
    expect(wait.next()).toBeNull()
    wait.reset()
    expect(wait.waiting).toBe(false)
    expect(wait.next()).toBe(REMOTE_UPDATE_POLL_MS)
  })
})
