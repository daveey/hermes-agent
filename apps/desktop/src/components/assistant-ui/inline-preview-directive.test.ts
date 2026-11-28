import { afterEach, describe, expect, it, vi } from 'vitest'

const readText = vi.hoisted(() => vi.fn())

vi.mock('@/lib/desktop-fs', async importOriginal => ({
  ...(await importOriginal<object>()),
  readDesktopFileText: readText
}))

import {
  collectThemeBridge,
  directiveFrameHeight,
  evaluateIntent,
  frameSizeFromMessage,
  INTENT_THROTTLE_MS,
  intentAckMessage,
  intentRejectNotice,
  intentScript,
  MAX_INTENT_LENGTH,
  nextFrameHeight,
  readInlineDoc,
  themePrelude,
  withInlineChrome
} from './inline-preview-directive'

describe('directiveFrameHeight', () => {
  it('returns null (auto-size) when absent or garbage', () => {
    expect(directiveFrameHeight(undefined)).toBeNull()
    expect(directiveFrameHeight('')).toBeNull()
    expect(directiveFrameHeight('tall')).toBeNull()
    expect(directiveFrameHeight('12.5')).toBeNull()
  })

  it('clamps an explicit height to the sane band', () => {
    expect(directiveFrameHeight('50')).toBe(120)
    expect(directiveFrameHeight('480')).toBe(480)
    expect(directiveFrameHeight('99999')).toBe(16000)
  })
})

describe('readInlineDoc', () => {
  afterEach(() => readText.mockReset())

  it('asks for the WHOLE file and returns it', async () => {
    const big = '<html><body>' + 'x'.repeat(700 * 1024) + '<h2>Section C</h2></body></html>'
    readText.mockResolvedValue({ byteSize: big.length, path: '/b.html', text: big, truncated: false })

    await expect(readInlineDoc('/b.html')).resolves.toBe(big)
    expect(readText).toHaveBeenCalledWith('/b.html', { full: true })
  })

  it('never frames a truncated document (older shell ignored `full`)', async () => {
    readText.mockResolvedValue({
      byteSize: 700 * 1024,
      path: '/b.html',
      text: '<img src="data:image/png;base64,iVBOR',
      truncated: true
    })

    await expect(readInlineDoc('/b.html')).resolves.toBeNull()
  })

  it('falls back on read errors and binary files', async () => {
    readText.mockResolvedValue({ error: 'too-large', message: 'File too large' })
    await expect(readInlineDoc('/b.html')).resolves.toBeNull()
    readText.mockResolvedValue({ binary: true, byteSize: 4, path: '/b.html', text: 'abcd' })
    await expect(readInlineDoc('/b.html')).resolves.toBeNull()
  })
})

describe('nextFrameHeight', () => {
  it('follows real content growth and shrink', () => {
    expect(nextFrameHeight(280, 4800, 0)).toEqual({ height: 4800, grow: 4520 })
    expect(nextFrameHeight(4800, 5200, 4520)).toEqual({ height: 5200, grow: 400 })
    expect(nextFrameHeight(5200, 900, 400)).toEqual({ height: 900, grow: 0 })
  })

  it('ignores rounding churn', () => {
    expect(nextFrameHeight(500, 503, 0)).toEqual({ height: 500, grow: 0 })
  })

  it('stops a viewport-coupled page (100vh + margin) from growing forever', () => {
    let height = 280
    let grow = 0

    // The page always answers "frame + 16 px".
    for (let i = 0; i < 50; i++) {
      ;({ height, grow } = nextFrameHeight(height, height + 16, grow))
    }

    expect(height).toBe(296)
  })
})

describe('withInlineChrome', () => {
  const prelude = themePrelude({ '--foreground': '#eee' }, 'Inter', 'dark')

  it('puts the theme prelude FIRST so page styles override it', () => {
    const doc = '<html><head><style>body{color:red}</style></head><body><h1>hi</h1></body></html>'
    const framed = withInlineChrome(doc, 'tok', prelude)

    expect(framed.startsWith(prelude)).toBe(true)
    expect(framed.indexOf(prelude)).toBeLessThan(framed.indexOf('color:red'))
  })

  it('injects the measuring script before </body>', () => {
    const doc = '<html><body><h1>hi</h1></body></html>'
    const framed = withInlineChrome(doc, 'tok', prelude)

    expect(framed.indexOf('postMessage')).toBeGreaterThan(framed.indexOf('<h1>'))
    expect(framed.indexOf('postMessage')).toBeLessThan(framed.indexOf('</body>'))
    expect(framed).toContain('"tok"')
  })

  it('appends the script when there is no body close tag', () => {
    const framed = withInlineChrome('<h1>fragment</h1>', 'tok', prelude)

    expect(framed).toContain('<h1>fragment</h1>')
    expect(framed).toContain('postMessage')
  })
})

describe('themePrelude', () => {
  it.each(['light', 'dark'] as const)('injects the app color scheme %s into the frame document', colorScheme => {
    expect(themePrelude({}, '', colorScheme)).toContain(`color-scheme:${colorScheme}`)
  })

  it('puts the injected color scheme where a page declaration overrides it', () => {
    const doc = '<html><head><style>:root{color-scheme:dark}</style></head><body><h1>hi</h1></body></html>'
    const framed = withInlineChrome(doc, 'tok', themePrelude({}, '', 'light'))

    // The injected default comes first; the page's own :root rule wins.
    expect(framed.indexOf('color-scheme:light')).toBeLessThan(framed.indexOf('color-scheme:dark'))
  })

  it('carries resolved tokens, transparent background, and the app font', () => {
    const prelude = themePrelude(
      { '--foreground': 'oklch(0.9 0 0)', '--accent': '#7aa2f7' },
      'Inter, sans-serif',
      'dark'
    )

    expect(prelude).toContain('--foreground:oklch(0.9 0 0)')
    expect(prelude).toContain('--accent:#7aa2f7')
    expect(prelude).toContain('background:transparent')
    expect(prelude).toContain('font-family:Inter, sans-serif')
  })

  it('omits the font rule when no font resolved', () => {
    expect(themePrelude({}, '', 'light')).not.toContain('font-family')
  })
})

describe('collectThemeBridge', () => {
  afterEach(() => {
    document.documentElement.className = ''
    delete document.documentElement.dataset.hermesMode
  })

  // #123048: the frame's color-scheme must come from the same resolved
  // appearance as the injected tokens, not from the separate `.dark` class
  // React's useIsDark() reads — that class can still hold last render's
  // value for a paint after applyTheme() has already updated data-hermes-mode.
  it('reads color-scheme from data-hermes-mode, not the .dark class', () => {
    document.documentElement.dataset.hermesMode = 'dark'
    document.documentElement.classList.remove('dark')

    expect(collectThemeBridge().colorScheme).toBe('dark')
  })

  it('falls back to light when the mode attribute disagrees the other way', () => {
    document.documentElement.dataset.hermesMode = 'light'
    document.documentElement.classList.add('dark')

    expect(collectThemeBridge().colorScheme).toBe('light')
  })
})

describe('frameSizeFromMessage', () => {
  const msg = (over: Record<string, unknown> = {}) => ({
    type: 'hermes-inline-preview-size',
    token: 'tok',
    height: 500,
    ...over
  })

  it('accepts our message with our token, height clamped', () => {
    expect(frameSizeFromMessage(msg(), 'tok')).toEqual({ height: 500 })
    expect(frameSizeFromMessage(msg({ height: 12 }), 'tok')?.height).toBe(120)
    // A tall board keeps its full height (the old 1200 px cap hid everything below section one).
    expect(frameSizeFromMessage(msg({ height: 5000 }), 'tok')?.height).toBe(5000)
    expect(frameSizeFromMessage(msg({ height: 99999 }), 'tok')?.height).toBe(16000)
    expect(frameSizeFromMessage(msg({ height: 500.7 }), 'tok')?.height).toBe(501)
  })

  it('rejects wrong type, wrong token, and hostile shapes', () => {
    expect(frameSizeFromMessage(msg({ type: 'other' }), 'tok')).toBeNull()
    expect(frameSizeFromMessage(msg({ token: 'stolen' }), 'tok')).toBeNull()
    expect(frameSizeFromMessage(msg({ height: 'tall' }), 'tok')).toBeNull()
    expect(frameSizeFromMessage(msg({ height: Infinity }), 'tok')).toBeNull()
    expect(frameSizeFromMessage(msg({ height: -5 }), 'tok')).toBeNull()
    expect(frameSizeFromMessage(null, 'tok')).toBeNull()
    expect(frameSizeFromMessage('str', 'tok')).toBeNull()
  })
})

describe('evaluateIntent', () => {
  const msg = (over: Record<string, unknown> = {}) => ({
    type: 'hermes-inline-preview-intent',
    token: 'tok',
    id: 7,
    prompt: 'get-price eth',
    ...over
  })

  const NOW = 100_000

  it('accepts our intent with our token, trimmed, carrying the call id', () => {
    expect(evaluateIntent(msg(), 'tok', NOW, 0)).toEqual({ id: 7, kind: 'accept', prompt: 'get-price eth' })
    expect(evaluateIntent(msg({ prompt: '  hi  ' }), 'tok', NOW, 0)).toMatchObject({ kind: 'accept', prompt: 'hi' })
  })

  it('accepts a prompt exactly at the cap untouched', () => {
    const prompt = 'x'.repeat(MAX_INTENT_LENGTH)

    expect(evaluateIntent(msg({ prompt }), 'tok', NOW, 0)).toMatchObject({ kind: 'accept', prompt })
  })

  // #118973: a sliced JSON payload reached the agent as garbage while the
  // widget believed it sent. Over-length is an explicit rejection now.
  it('rejects an over-length prompt instead of truncating it', () => {
    const payload = 'FC-FLUSH ' + JSON.stringify(Array.from({ length: 30 }, (_, i) => ({ card: i, rating: 3 })))

    expect(payload.length).toBeGreaterThan(MAX_INTENT_LENGTH)
    expect(evaluateIntent(msg({ prompt: payload }), 'tok', NOW, 0)).toEqual({
      ack: { error: 'too_long', maxLength: MAX_INTENT_LENGTH, ok: false },
      id: 7,
      kind: 'reject'
    })
  })

  it('rejects a throttled intent with a retry hint instead of dropping it', () => {
    expect(evaluateIntent(msg(), 'tok', NOW, NOW - 300)).toEqual({
      ack: { error: 'throttled', ok: false, retryAfterMs: INTENT_THROTTLE_MS - 300 },
      id: 7,
      kind: 'reject'
    })
    expect(evaluateIntent(msg(), 'tok', NOW, NOW - INTENT_THROTTLE_MS)).toMatchObject({ kind: 'accept' })
  })

  it('rejects an empty or non-string prompt from our frame as invalid', () => {
    expect(evaluateIntent(msg({ prompt: '   ' }), 'tok', NOW, 0)).toMatchObject({ ack: { error: 'invalid' } })
    expect(evaluateIntent(msg({ prompt: 42 }), 'tok', NOW, 0)).toMatchObject({ ack: { error: 'invalid' } })
  })

  it('drops a non-integer id so the ack cannot be aimed with a hostile value', () => {
    expect(evaluateIntent(msg({ id: 'x' }), 'tok', NOW, 0)).toMatchObject({ id: null, kind: 'accept' })
    expect(evaluateIntent(msg({ id: 1.5 }), 'tok', NOW, 0)).toMatchObject({ id: null })
  })

  it('ignores wrong token, wrong type, and hostile shapes without a reply', () => {
    expect(evaluateIntent(msg({ token: 'stolen' }), 'tok', NOW, 0)).toBeNull()
    expect(evaluateIntent(msg({ type: 'hermes-inline-preview-size' }), 'tok', NOW, 0)).toBeNull()
    expect(evaluateIntent(null, 'tok', NOW, 0)).toBeNull()
    expect(evaluateIntent('str', 'tok', NOW, 0)).toBeNull()
  })
})

describe('intentScript', () => {
  // Run the injected script against a fake parent so the frame side of the
  // protocol is exercised for real: what it posts and what send() resolves.
  function mountFrame() {
    const posted: Array<Record<string, unknown>> = []
    const listeners: Record<string, Array<(e: unknown) => void>> = {}
    const parent = { postMessage: (data: Record<string, unknown>) => posted.push(data) }
    const win: { hermes?: { maxLength: number; send: (p: unknown) => Promise<unknown> } } = {}

    const addEventListener = (type: string, fn: (e: unknown) => void) => {
      ;(listeners[type] ??= []).push(fn)
    }

    const body = intentScript('tok')
      .replace(/^<script>/, '')
      .replace(/<\/script>$/, '')

    new Function('parent', 'addEventListener', 'window', body)(parent, addEventListener, win)

    const reply = (data: unknown, source: unknown = parent) => listeners.message?.forEach(fn => fn({ data, source }))

    return { posted, reply, send: win.hermes!.send, win }
  }

  it('posts the full prompt, never a truncated one', () => {
    const { posted, send } = mountFrame()
    const long = 'y'.repeat(3200)

    void send(long)

    expect(posted).toHaveLength(1)
    expect(posted[0]).toMatchObject({ prompt: long, token: 'tok', type: 'hermes-inline-preview-intent' })
  })

  it('resolves send() with the parent ack for that call', async () => {
    const { posted, reply, send } = mountFrame()
    const pending = send('hello')
    const id = posted[0].id as number

    reply(intentAckMessage('tok', id, { error: 'too_long', maxLength: 500, ok: false }))

    await expect(pending).resolves.toEqual({ error: 'too_long', maxLength: 500, ok: false })
  })

  it('ignores acks with the wrong token or from a non-parent source', async () => {
    vi.useFakeTimers()

    try {
      const { posted, reply, send } = mountFrame()
      const pending = send('hello')
      const id = posted[0].id as number

      reply(intentAckMessage('stolen', id, { ok: true }))
      reply(intentAckMessage('tok', id, { ok: true }), {})
      vi.advanceTimersByTime(5000)

      await expect(pending).resolves.toEqual({ error: 'undelivered', ok: false })
    } finally {
      vi.useRealTimers()
    }
  })

  it('resolves invalid for an empty prompt without posting, and exposes the cap', async () => {
    const { posted, send, win } = mountFrame()

    await expect(send('   ')).resolves.toEqual({ error: 'invalid', ok: false })
    expect(posted).toHaveLength(0)
    expect(win.hermes?.maxLength).toBe(MAX_INTENT_LENGTH)
  })
})

// desktop-13785ebc41: a rating page sent every rating in one over-length
// intent and ignored the refusal, so the user saw nothing while all of it was
// lost. A refusal must reach the user, not only the page.
describe('intentRejectNotice', () => {
  it('names an over-length refusal for the user', () => {
    const notice = intentRejectNotice({ error: 'too_long', maxLength: MAX_INTENT_LENGTH, ok: false })

    expect(notice).toContain('not sent')
    expect(notice).toContain(String(MAX_INTENT_LENGTH))
  })

  it('names invalid and undelivered refusals', () => {
    expect(intentRejectNotice({ error: 'invalid', ok: false })).toContain('not sent')
    expect(intentRejectNotice({ error: 'undelivered', ok: false })).toContain('not sent')
  })

  it('stays quiet for accepted and throttled intents (a good widget retries those)', () => {
    expect(intentRejectNotice({ ok: true })).toBeNull()
    expect(intentRejectNotice({ error: 'throttled', ok: false, retryAfterMs: 400 })).toBeNull()
  })
})
