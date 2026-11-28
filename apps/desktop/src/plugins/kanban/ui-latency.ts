import { cancelUiLatency, finishUiLatencyAfterPaint } from '@hermes/plugin-sdk'
import { useEffect } from 'react'

/**
 * `ui.card_read` finisher for the task drawer: ends the span two frames after the drawer COMMITTED the detail
 * of `cardId` (`loaded`), and cancels it when the read failed or the drawer closed first. The request
 * completing is not the paint, so nothing here fires from the fetch.
 */
export function useCardReadLatency(cardId: null | string, loaded: boolean, failed: boolean): void {
  useEffect(() => {
    if (!cardId) {
      return undefined
    }

    if (failed) {
      cancelUiLatency('ui.card_read')

      return undefined
    }

    if (!loaded) {
      return undefined
    }

    // The cleanup runs on unmount (drawer closed) and on any dep change: either way the result is gone.
    let shown = true
    finishUiLatencyAfterPaint('ui.card_read', { card_id: cardId }, () => shown)

    return () => {
      shown = false
    }
  }, [cardId, failed, loaded])
}
