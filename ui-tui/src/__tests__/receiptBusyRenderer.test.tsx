import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import React, { act, useEffect } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { turnController } from '../app/turnController.js'
import { $uiState, getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'
import { useQueue } from '../hooks/useQueue.js'

function deferred() {
  let resolve!: (value: any) => void
  let reject!: (error: Error) => void
  const promise = new Promise<any>((yes, no) => {
    resolve = yes
    reject = no
  })

  return {
    promise,
    resolve(value: any) {
      resolve(value)

      return promise
    },
    reject(error: Error) {
      reject(error)

      return promise.catch(() => {})
    }
  }
}

async function mount() {
  // macOS: tmpdir() is /var/..., a symlink to /private/var; captureDestination() realpaths
  // HERMES_HOME, so receipts must carry the canonical home or target_profile_home never matches.
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'ink-receipt-busy-')))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()

  const info = {
    model: 'test',
    skills: {},
    tools: {},
    profile_name: 'default',
    stored_session_id: 'stored-owner',
    execution_epoch: 'owner-epoch',
    execution_generation: 1,
    running: false
  }

  patchUiState({ sid: 'owner', info, busy: false, status: 'ready' })
  const submits: ReturnType<typeof deferred>[] = []
  const snapshots: ReturnType<typeof deferred>[] = []

  const request = vi.fn((method: string) => {
    if (method === 'input.detect_drop') {
      return Promise.resolve({ matched: false })
    }
    const pending = deferred()

    if (method === 'prompt.submit') {
      submits.push(pending)
    } else if (method === 'session.activate') {
      snapshots.push(pending)
    } else {
      throw new Error(`unexpected RPC: ${method}`)
    }

    return pending.promise
  })

  let queue!: ReturnType<typeof useQueue>
  let submission!: ReturnType<typeof useSubmission>

  const noop = () => {}

  const onEvent = createGatewayEventHandler({
    composer: { dequeue: () => undefined, queueEditRef: { current: null }, sendQueued: noop, setInput: noop },
    gateway: { gw: { request }, rpc: async () => null },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: { current: 80 },
      newSession: noop,
      resetSession: noop,
      resumeById: noop,
      setCatalog: noop
    },
    submission: { submitRef: { current: noop } },
    system: { bellOnComplete: false, sys: noop },
    transcript: { appendMessage: noop, panel: noop, setHistoryItems: noop },
    voice: { setProcessing: noop, setRecording: noop, setVoiceEnabled: noop }
  } as any)

  const emit = (type: string, generation: number) =>
    onEvent({
      type,
      session_id: 'owner',
      payload: { execution_epoch: info.execution_epoch, execution_generation: generation, text: 'completed' }
    } as any)

  function Harness() {
    const ui = useStore($uiState)
    queue = useQueue()
    submission = useSubmission({
      appendMessage: noop,
      composerActions: { ...queue, prependQueue: queue.prependQ, takeQueue: queue.takeQ } as any,
      composerRefs: { queueRef: queue.queueRef, queueEditRef: queue.queueEditRef, tokensRef: { current: [] } } as any,
      composerState: { input: '', inputBuf: [], completions: [] } as any,
      gw: { request } as any,
      setLastUserMsg: noop,
      slashRef: { current: () => true },
      submitRef: { current: noop },
      sys: noop
    })
    useEffect(() => {
      if (!ui.sid || ui.busy || queue.queueEditRef.current !== null || !queue.queueRef.current.length) {
        return
      }
      const next = queue.dequeue()

      if (next) {
        submission.sendQueued(next)
      }
    }, [ui.sid, ui.busy, queue, submission])

    return (
      <Text>
        {ui.status} {queue.queuedDisplay.join('|')}
      </Text>
    )
  }

  let instance!: ReturnType<typeof renderSync>
  let disposed = false

  const harness = {
    submits,
    snapshots,
    request,
    emit,
    get queue() {
      return queue
    },
    get submission() {
      return submission
    },
    receipt: (id: string, status: string) => ({
      admission_id: id,
      target_session_id: info.stored_session_id,
      target_profile_home: home,
      status
    }),
    snapshot: (running: boolean, generation: number) => ({
      session_id: 'owner',
      session_key: info.stored_session_id,
      running,
      info: { ...info, running, execution_generation: generation }
    }),
    cleanup() {
      if (disposed) {
        return
      }
      disposed = true

      try {
        instance?.unmount()
      } finally {
        turnController.fullReset()
        resetUiState()
        vi.unstubAllEnvs()
        rmSync(home, { recursive: true, force: true })
      }
    }
  }

  // Register with the runner before the first await: a test timeout does not
  // cancel its body, so a finally there could reset the next test's live store.
  cleanups.push(() => harness.cleanup())
  await act(async () => {
    instance = renderSync(<Harness />, {
      stdin: new PassThrough() as any,
      stdout: Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false }) as any,
      stderr: new PassThrough() as any,
      patchConsole: false
    })
  })

  return harness
}

const cleanups: Array<() => void> = []
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
})
afterEach(async () => {
  try {
    await act(async () => {
      const errors: unknown[] = []

      for (const cleanup of cleanups.splice(0)) {
        try {
          cleanup()
        } catch (error) {
          errors.push(error)
        }
      }

      if (errors.length) {
        throw new AggregateError(errors, 'receipt harness cleanup failed')
      }
    })
  } finally {
    vi.unstubAllGlobals()
  }
})

it('keeps a new harness intact when an already-disposed cleanup runs again', async () => {
  const old = await mount()
  const release = deferred()
  const suspended = release.promise.then(() => old.cleanup())

  await act(async () => old.cleanup())
  await mount()
  const current = getUiState()
  const home = process.env.HERMES_HOME
  await act(async () => {
    await release.resolve(undefined)
    await suspended
  })

  expect(getUiState()).toBe(current)
  expect(process.env.HERMES_HOME).toBe(home)
})

it.each(['terminal', 'unknown'])(
  'reconciles %s retries to idle so queued follow-ups drain without another terminal event',
  async status => {
    const h = await mount()
    const item = await act(async () => h.queue.stage('original'))

    await act(async () => h.submission.sendQueued(item))
    expect(h.submits).toHaveLength(1)
    await act(async () => {
      h.emit('message.start', 1)
      h.emit('message.complete', 1)
      await h.submits[0]!.reject(new Error('lost acknowledgement'))
    })
    expect(item.failed).toBe(true)
    await act(async () => h.submission.sendQueued(h.queue.dequeue(true)!))
    expect(h.submits).toHaveLength(2)
    await act(async () => h.submits[1]!.resolve(h.receipt(item.submissionId!, status)))
    expect(h.queue.queueRef.current).toHaveLength(0)
    expect(h.snapshots).toHaveLength(1)
    await act(async () => h.snapshots[0]!.resolve(h.snapshot(false, 1)))
    expect(getUiState()).toMatchObject({ busy: false, status: 'ready' })
    await act(async () => h.queue.enqueue('follow-up'))
    expect(h.submits).toHaveLength(3)
    expect(h.request).toHaveBeenLastCalledWith('prompt.submit', expect.objectContaining({ text: 'follow-up' }))
  }
)

it.each([false, true])(
  'preserves a newer running generation across old receipts (delayed idle snapshot: %s)',
  async delayed => {
    const h = await mount()
    const item = await act(async () => h.queue.stage('retry old identity'))

    await act(async () => h.submission.sendQueued(item))
    expect(h.submits).toHaveLength(1)
    await act(async () => {
      if (!delayed) {
        h.emit('message.start', 2)
      }
      await h.submits[0]!.resolve(h.receipt(item.submissionId!, 'terminal'))
    })
    expect(h.queue.queueRef.current).toHaveLength(0)
    expect(h.snapshots).toHaveLength(1)
    await act(async () => {
      if (delayed) {
        h.emit('message.start', 2)
      }
      await h.snapshots[0]!.resolve(h.snapshot(!delayed, delayed ? 1 : 2))
    })
    expect(getUiState()).toMatchObject({ busy: true, status: 'running…', info: { execution_generation: 2 } })
    // Flush the actual drain effect before the negative assertion, not a 20ms guess.
    await act(async () => h.queue.enqueue('must wait'))
    expect(h.submits).toHaveLength(1)
    expect(h.queue.queueRef.current).toHaveLength(1)
    await act(async () => h.emit('message.complete', 2))
    expect(h.submits).toHaveLength(2)
  }
)
