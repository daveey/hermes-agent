/** Mounted pane bodies, including non-scriptable file previews. Open requests
 * acknowledge a real surface, not just a tab stored for a future render. */
const mounted = new Set<string>()

export function registerPreviewPaneMount(tabId: string): () => void {
  mounted.add(tabId)

  return () => {
    mounted.delete(tabId)
  }
}

export function hasPreviewPaneMount(tabId: string): boolean {
  return mounted.has(tabId)
}
