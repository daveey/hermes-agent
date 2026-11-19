import { registry } from '@/contrib/registry'
import { $tabStripDefault } from '@/store/tabstrip-prefs'

import { allPaneIds, findGroup, findGroupOfPane, type LayoutNode, removePane } from './model'
import { setTreePaneParked } from './parked-panes'
import { singleTabMemory as memory, type ParkedTab } from './single-tab-memory'
const on = () => $tabStripDefault.get() === 'single'

const isMain = (id: string) =>
  (registry.getArea('panes').find(c => c.id === id)?.data as { placement?: string } | undefined)?.placement === 'main'

/** Keep runtime owners alive, but remove displaced main tabs from the layout. */
function enforce(tree: LayoutNode): LayoutNode {
  if (!on()) {
    return tree
  }

  const walk = (node: LayoutNode): LayoutNode => {
    if (node.type !== 'group') {
      const children = node.children.map(walk)

      return children.some((c, i) => c !== node.children[i]) ? { ...node, children } : node
    }

    const mains = node.panes.filter(isMain)

    if (mains.length < 2) {
      return node
    }

    const keep =
      memory.preferred && mains.includes(memory.preferred)
        ? memory.preferred
        : mains.includes(node.active)
          ? node.active
          : mains[0]

    const displaced = mains.filter(id => id !== keep)

    for (const id of displaced) {
      memory.parked = [
        ...memory.parked.filter(e => e.id !== id),
        { groupId: node.id, id, index: node.panes.indexOf(id) }
      ]
      setTreePaneParked(id, true)
    }

    const panes = node.panes.filter(id => !displaced.includes(id))

    return { ...node, panes, active: memory.preferred === keep || !panes.includes(node.active) ? keep : node.active }
  }

  return walk(tree)
}

function unpark(id: string): ParkedTab | undefined {
  const entry = memory.parked.find(e => e.id === id)

  if (entry) {
    memory.parked = memory.parked.filter(e => e.id !== id)
    setTreePaneParked(id, false)
  }

  return entry
}

function restore(tree: LayoutNode, id: string, entry: ParkedTab | undefined): LayoutNode | null {
  const zone = entry ? findGroup(tree, entry.groupId) : null

  if (!zone || !entry || !registry.getArea('panes').some(c => c.id === id)) {
    return null
  }
  const at = Math.min(entry.index, zone.panes.length)
  const panes = [...zone.panes.slice(0, at), id, ...zone.panes.slice(at)]

  const put = (node: LayoutNode): LayoutNode =>
    node.type === 'group'
      ? node.id === zone.id
        ? { ...node, panes, active: id }
        : node
      : { ...node, children: node.children.map(put) }

  return put(tree)
}

/** Refill a closed main slot without terminating its displaced session owners. */
function remove(tree: LayoutNode, id: string): LayoutNode | null {
  const zone = findGroupOfPane(tree, id)
  const registered = new Set(registry.getArea('panes').map(c => c.id))

  if (on() && zone && (isMain(id) || !registered.has(id)) && !zone.panes.some(p => p !== id && isMain(p))) {
    const inTree = new Set(allPaneIds(tree))

    const candidate = [...memory.parked]
      .reverse()
      .find(e => e.groupId === zone.id && registered.has(e.id) && !inTree.has(e.id))

    if (candidate) {
      unpark(candidate.id)

      const swap = (node: LayoutNode): LayoutNode =>
        node.type === 'group'
          ? node.id === zone.id
            ? {
                ...node,
                panes: node.panes.map(p => (p === id ? candidate.id : p)),
                active: node.active === id ? candidate.id : node.active
              }
            : node
          : { ...node, children: node.children.map(swap) }

      return swap(tree)
    }
  }

  return removePane(tree, id)
}

function release(tree: LayoutNode | null, dismissed: ReadonlySet<string>): LayoutNode | null {
  const entries = [...memory.parked].sort((a, b) => a.index - b.index)
  memory.parked = []
  entries.forEach(e => setTreePaneParked(e.id, false))

  if (!tree) {
    return null
  }
  const registered = new Set(registry.getArea('panes').map(c => c.id))
  let next = tree

  for (const entry of entries) {
    if (!registered.has(entry.id) || allPaneIds(next).includes(entry.id) || dismissed.has(entry.id)) {
      continue
    }

    const insert = (node: LayoutNode): LayoutNode =>
      node.type === 'group'
        ? node.id === entry.groupId
          ? { ...node, panes: [...node.panes.slice(0, entry.index), entry.id, ...node.panes.slice(entry.index)] }
          : node
        : { ...node, children: node.children.map(insert) }

    next = insert(next)
  }

  return next
}

function bind(
  read: () => LayoutNode | null,
  commit: (tree: LayoutNode) => void,
  adopt: () => void,
  dismissed: () => ReadonlySet<string>
) {
  let previous = on()

  return $tabStripDefault.listen(() => {
    const current = on()

    if (current === previous) {
      return
    }
    previous = current
    const tree = read()

    if (current) {
      if (tree) {
        commit(tree)
      }
    } else if (memory.parked.length) {
      const released = release(tree, dismissed())

      if (released) {
        commit(released)
      }
      adopt()
    }
  })
}

function reveal(tree: LayoutNode, id: string, commit: (tree: LayoutNode) => void, adopt: () => void) {
  const displaced = unpark(id)

  if (findGroupOfPane(tree, id)) {
    return
  }
  memory.preferred = id

  try {
    const restored = restore(tree, id, displaced)

    if (restored) {
      commit(restored)
    } else {
      adopt()
    }
  } finally {
    memory.preferred = null
  }
}

export const singleTabs = { enforce, remove, reveal, bind }
