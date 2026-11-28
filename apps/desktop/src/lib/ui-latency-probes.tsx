/**
 * Render-less finishers for `lib/ui-latency.ts`. Each mounts inside the surface whose paint ends a span,
 * so its effect runs only after React committed that surface; the helper then waits two frames more.
 * They subscribe to `$uiLatencyEpoch` so a click on what is ALREADY on screen still finishes.
 */

import { useAuiState } from '@assistant-ui/react'
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
  const hasRows = useAuiState(s => s.thread.messages.length > 0)
  const ready = usePaneVisible() && !loading && hasRows
  const latest = useRef({ ready, storedSessionId })
  latest.current = { ready, storedSessionId }

  useEffect(() => {
    if (!ready || !storedSessionId) {
      return
    }

    finishUiLatencyAfterPaint(
      'ui.session_switch',
      { session_id: storedSessionId },
      () => latest.current.ready && latest.current.storedSessionId === storedSessionId
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

  useEffect(() => {
    const span = peekUiLatency('ui.new_session')

    if (!span || disabled || !paneVisible || !editorShown(editorRef.current)) {
      return
    }

    if (span.expect.runtimeId !== undefined && span.expect.runtimeId !== bound) {
      return
    }

    finishUiLatencyAfterPaint('ui.new_session', {}, () => editorShown(editorRef.current))
  }, [bound, disabled, editorRef, epoch, paneVisible])

  return null
}
