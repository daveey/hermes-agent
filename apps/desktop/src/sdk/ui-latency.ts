/**
 * `desktop.ui_latency` span controls for bundled plugins (the Kanban board times card read and create).
 * Same helper the core surfaces use (`@/lib/ui-latency`): see it for the cancel / supersede / paint rules.
 * Consumers read the result with `host.onEvent('desktop.ui_latency', …)`.
 */
export {
  cancelUiLatency,
  completeUiLatency,
  finishUiLatencyAfterPaint,
  startUiLatency,
  type UiLatencyIds,
  type UiLatencyName,
  type UiLatencyToken
} from '@/lib/ui-latency'
