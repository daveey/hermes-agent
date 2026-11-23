interface ResetWindow {
  isDestroyed: () => boolean
  webContents: { isDestroyed: () => boolean; send: (channel: string, payload: unknown) => void }
}

// Each renderer owns its timeout state. Broadcast the reset observation; only
// timeout recovery listeners act on it. Healthy windows never reload or redial.
export function broadcastBootstrapReset(windows: ResetWindow[], progress: { phase: string }): void {
  for (const window of windows) {
    if (window.isDestroyed() || window.webContents.isDestroyed()) {
      continue
    }

    window.webContents.send('hermes:boot-progress', { ...progress, phase: 'bootstrap.reset' })
  }
}
