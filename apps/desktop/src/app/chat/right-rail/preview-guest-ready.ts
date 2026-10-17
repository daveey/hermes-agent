/** Whether a pane's live guest can answer an agent yet. The page reader and
 *  script runner are registered the moment the pane mounts, long before
 *  Electron attaches the guest and fires dom-ready; an `open` acknowledged on
 *  registration alone hands the agent a page that throws "not ready" (or runs
 *  script into a document that is about to be replaced). The pane publishes
 *  the guest's real lifecycle here; `epoch` advances on every change so a
 *  waiter can tell a fresh dom-ready from the previous page's. */
export type PreviewGuestState =
  | { epoch: number; status: 'failed'; error: string }
  | { epoch: number; status: 'loading' }
  | { epoch: number; status: 'ready' }

type GuestStatus = { status: 'failed'; error: string } | { status: 'loading' } | { status: 'ready' }

const states = new Map<string, PreviewGuestState>()
let epoch = 0

export function setPreviewGuestState(tabId: string, next: GuestStatus): void {
  epoch += 1
  states.set(tabId, { ...next, epoch })
}

/** A navigation that needed no load (the guest already shows the address)
 *  still answers a waiter for "the page after this open". */
export function touchPreviewGuestReady(tabId: string): void {
  if (states.get(tabId)?.status === 'ready') {
    setPreviewGuestState(tabId, { status: 'ready' })
  }
}

export function clearPreviewGuestState(tabId: string): void {
  states.delete(tabId)
}

export function previewGuestState(tabId: string): PreviewGuestState | undefined {
  return states.get(tabId)
}

/** The current epoch counter, for a waiter to snapshot before it acts. */
export function previewGuestEpoch(): number {
  return epoch
}
