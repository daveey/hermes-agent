import type { TabStripMode } from '@/components/pane-shell/tree/model'
import { type Codec, persistentAtom } from '@/lib/persisted'

const TAB_STRIP_DEFAULT_STORAGE_KEY = 'hermes.desktop.tabStripDefault'

/** Single limits main-area tabs; the other choices control strip chrome. */
export type TabStripDefault = 'single' | 'auto' | TabStripMode

const codec: Codec<TabStripDefault> = {
  decode: raw => (raw === 'auto' || raw === 'always' || raw === 'never' ? raw : 'single'),
  encode: value => (value === 'single' ? null : value)
}

/**
 * Single keeps one main-area tab per zone without closing its background
 * sessions/guests. Side-panel tabs retain Auto behavior. Explicit strip choices
 * continue to win for Auto/Always/Never; Single's main-slot policy is separate
 * from those chrome preferences.
 */
export const $tabStripDefault = persistentAtom<TabStripDefault>(TAB_STRIP_DEFAULT_STORAGE_KEY, 'single', codec)

export function setTabStripDefault(value: TabStripDefault) {
  $tabStripDefault.set(value)
}

/** The mode a zone resolves against: its own choice, else the app default. */
export function effectiveTabStripMode(zoneMode: TabStripMode | undefined): TabStripMode | undefined {
  if (zoneMode) {
    return zoneMode
  }

  const fallback = $tabStripDefault.get()

  return fallback === 'auto' || fallback === 'single' ? undefined : fallback
}
