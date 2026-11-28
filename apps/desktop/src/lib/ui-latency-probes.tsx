/**
 * Render-less finishers for `lib/ui-latency.ts`. Each mounts inside the surface whose paint ends a span,
 * so its effect runs only after React committed that surface; the helper then waits two frames more.
 * They subscribe to `$uiLatencyEpoch` so a click on what is ALREADY on screen still finishes.
 */

import { useStore } from '@nanostores/react'
import { type RefObject, useEffect, useRef } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { $uiLatencyEpoch, finishUiLatencyAfterPaint, peekUiLatency } from '@/lib/ui-latency'

/**
 * The transcript of `storedSessionId` has rows committed in a visible pane that is no longer loading.
 * Mounted beside the message list in the same commit, so its effect runs after the rows' commit.
 */
export function UiLatencyTranscriptProbe({
  loading,
  storedSessionId
}: {
  loading: boolean
  storedSessionId?: null | string
}) {
  const epoch = useStore($uiLatencyEpoch)
  // Empty loaded transcripts count too. The caller supplies the runtime's stored identity (not the
  // selected route), and its loading gate covers route/runtime mismatch and pending hydration.
  const ready = usePaneVisible() && !loading
  const latest = useRef({ ready, storedSessionId })
  latest.current = { ready, storedSessionId }
  const mounted = useRef(false)

  // eslint-disable-next-line no-restricted-syntax -- Mount lifetime only, not a reactive mirror; supports StrictMode replay.
  useEffect(() => {
    mounted.current = true

    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (!ready || !storedSessionId) {
      return
    }

    finishUiLatencyAfterPaint(
      'ui.session_switch',
      { session_id: storedSessionId },
      () => mounted.current && latest.current.ready && latest.current.storedSessionId === storedSessionId
    )
  }, [epoch, ready, storedSessionId])

  return null
}

const editorShown = (editor: HTMLElement | null): boolean =>
  Boolean(
    editor?.isConnected &&
    editor.isContentEditable !== false &&
    editor.getAttribute('contenteditable') !== 'false' &&
    editor.getClientRects().length > 0 &&
    !editor.closest('[data-pane-hidden]')
  )

/** A visible, editable composer bound to the session the new-session click asked for. */
export function UiLatencyComposerProbe({
  disabled,
  editorRef,
  runtimeId
}: {
  disabled: boolean
  editorRef: RefObject<HTMLElement | null>
  runtimeId?: null | string
}) {
  const epoch = useStore($uiLatencyEpoch)
  const paneVisible = usePaneVisible()
  const bound = runtimeId ?? null
  const latest = useRef({ bound, disabled, paneVisible })
  latest.current = { bound, disabled, paneVisible }

  useEffect(() => {
    const span = peekUiLatency('ui.new_session')

    if (!span || disabled || !paneVisible || !editorShown(editorRef.current)) {
      return
    }

    if (span.expect.runtimeId !== undefined && span.expect.runtimeId !== bound) {
      return
    }

    const expected = span.expect.runtimeId

    // After the frames re-read the CURRENT props: the composer may be disabled, hidden or rebound by then.
    finishUiLatencyAfterPaint('ui.new_session', {}, () => {
      const now = latest.current

      return (
        !now.disabled &&
        now.paneVisible &&
        now.bound === bound &&
        (expected === undefined || expected === now.bound) &&
        editorShown(editorRef.current)
      )
    })
  }, [bound, disabled, editorRef, epoch, paneVisible])

  return null
}
