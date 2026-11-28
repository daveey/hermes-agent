import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'

const startup = vi.hoisted(() => ({ image: '' }))
vi.mock('../config/env.js', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  get STARTUP_IMAGE() {
    return startup.image
  },
  STARTUP_QUERY: 'literal caption'
}))

const cleanups: Array<() => void> = []
afterEach(() => {
  resetUiState()
  vi.unstubAllEnvs()

  for (const cleanup of cleanups.splice(0)) {
    cleanup()
  }
})

it.each([true, false])(
  'startup image bytes travel with the literal prompt and attachment failure prevents a text-only run (valid: %s)',
  async valid => {
    const home = mkdtempSync(join(tmpdir(), 'ink-startup-image-'))
    cleanups.push(() => rmSync(home, { recursive: true, force: true }))
    vi.stubEnv('HERMES_HOME', home)
    vi.stubEnv('HERMES_TUI_GATEWAY_URL', '')
    startup.image = join(home, 'startup.png')
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nL8AAAAASUVORK5CYII=',
      'base64'
    )
    writeFileSync(startup.image, png)
    resetUiState()
    patchUiState({ sid: 'owner' })

    if (!valid) {
      rmSync(startup.image)
    }
    // Staging includes a real fsync. Await the outcome, not a one-second polling race
    // with the disk; the unchanged runner test timeout still guards a hung startup.
    const completed = Promise.withResolvers<void>()
    const submit = vi.fn((_text: string, _attachments: Array<{ path: string; mime: string }>) => completed.resolve())
    const sys = vi.fn(() => completed.resolve())
    const request = vi.fn(async (_method: string, _params?: unknown) => ({}))

    const handler = createGatewayEventHandler({
      gateway: { gw: { isCanonical: true, request }, rpc: request },
      composer: { setInput: vi.fn(), enqueue: vi.fn() },
      session: {
        STARTUP_RESUME_ID: 'owner',
        colsRef: { current: 80 },
        resumeById: vi.fn(),
        newSession: vi.fn(),
        resetSession: vi.fn(),
        setCatalog: vi.fn()
      },
      submission: { submitLiteralRef: { current: submit }, submitRef: { current: vi.fn() } },
      system: { sys, bellOnComplete: false, bellOnPrompt: false },
      transcript: { appendMessage: vi.fn(), panel: vi.fn(), setHistoryItems: vi.fn() },
      voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
    } as any)

    handler({ type: 'gateway.ready', payload: {} } as any)
    await completed.promise

    if (valid) {
      expect(submit).toHaveBeenCalledWith('literal caption', [{ path: expect.any(String), mime: 'image/png' }])
      expect(readFileSync(submit.mock.calls[0]![1][0].path)).toEqual(png)
    } else {
      expect(sys).toHaveBeenCalledWith(expect.stringContaining('startup image attach failed'))
      expect(submit).not.toHaveBeenCalled()
    }

    expect(request.mock.calls.some(([method]) => method === 'image.attach')).toBe(false)
  }
)
