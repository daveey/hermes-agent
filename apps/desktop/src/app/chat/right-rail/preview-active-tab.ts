/**
 * THE PREVIEW AN AGENT TOOL ACTS ON — one resolution shared by read_preview
 * (preview-reader.ts), drive_preview (the nav / input / script-runner
 * registries) and the preview tour, so a read and the action after it can
 * never land on different tabs.
 *
 * Callers pass the tabs the requesting session may see
 * (`previewTabsFor(owner)`): a background session's agent stays inside its own
 * tabs even while another session holds focus (#73890).
 */

import { findGroup } from '@/components/pane-shell/tree/model'
import { $activeTreeGroup, $hoveredTreeGroup, $layoutTree } from '@/components/pane-shell/tree/store'
import { $rightRailActiveTabId } from '@/store/layout'
import { $visiblePreviewTabs, type PreviewTab, previewTabsFor, rememberedPreviewTabId } from '@/store/preview'
import { explicitOpenBlocksZone, PREVIEW_TILE_PREFIX } from '@/store/preview-explicit'
import { ownerIdentity, type PreviewOwner } from '@/store/preview-ownership'
import { isBrowserWindow } from '@/store/windows'

function tabIdFromPreviewPane(paneId: string | undefined): null | string {
  if (!paneId?.startsWith(`${PREVIEW_TILE_PREFIX}:`)) {
    return null
  }

  return paneId.slice(PREVIEW_TILE_PREFIX.length + 1)
}

/** Active preview tab in a layout zone, if that tab is still open. */
function openTabInGroup(groupId: null | string, tabs: readonly PreviewTab[]): null | PreviewTab {
  const tree = $layoutTree.get()

  if (!tree || !groupId) {
    return null
  }

  const tabId = tabIdFromPreviewPane(findGroup(tree, groupId)?.active)

  if (!tabId) {
    return null
  }

  return tabs.find(tab => tab.id === tabId) ?? null
}

/**
 * The preview the user is looking at: hovered zone, else focused zone, else
 * the store. A focused zone that is still the pre-open zone does not override
 * an explicit open living in a different group — that is follow()'s clobber,
 * not a look.
 */
export function resolveActivePreviewTab(tabs: readonly PreviewTab[] = previewTabsFor()): null | PreviewTab {
  if (tabs.length === 0) {
    return null
  }

  const hovered = openTabInGroup($hoveredTreeGroup.get(), tabs)

  if (hovered) {
    return hovered
  }

  const focusedId = $activeTreeGroup.get()
  const focused = openTabInGroup(focusedId, tabs)
  const openIds = tabs.map(tab => tab.id)

  if (focused && !explicitOpenBlocksZone(focusedId, openIds)) {
    return focused
  }

  return tabs.find(tab => tab.id === $rightRailActiveTabId.get()) ?? tabs[0] ?? null
}

/**
 * The page a HIDDEN session's agent works in: one of that session's OWN tabs
 * while none of them is on screen (the user switched chats; the guest stays
 * mounted, parked and inert — see parked-panes.ts). Never a pinned tab: pins
 * render in every session, so the user may be looking at one. Null when the
 * owner's tabs are on screen (normal focus/hover resolution applies), when it
 * owns no tab, or in a popped-out Browser window (it answers for its one tab).
 * Prefers the tab the session last had in front, then its first web page.
 */
export function backgroundPreviewTabFor(owner: PreviewOwner | undefined): null | PreviewTab {
  if (owner === undefined || isBrowserWindow()) {
    return null
  }

  const own = previewTabsFor(owner).filter(tab => !tab.pinned)

  if (own.length === 0) {
    return null
  }

  const onScreen = new Set($visiblePreviewTabs.get().map(tab => tab.id))

  if (own.some(tab => onScreen.has(tab.id))) {
    return null
  }

  const remembered = rememberedPreviewTabId(ownerIdentity(owner).sessionId)

  return own.find(tab => tab.id === remembered) ?? own.find(tab => tab.target.kind === 'url') ?? own[0] ?? null
}

/** The active preview among the tabs `owner` (a stored id; null = a session
 *  with none yet; omitted = the focused session) may see. A hidden session
 *  resolves to its own parked page (backgroundPreviewTabFor). */
export function activePreviewTabFor(owner?: PreviewOwner): null | PreviewTab {
  return backgroundPreviewTabFor(owner) ?? resolveActivePreviewTab(previewTabsFor(owner))
}
