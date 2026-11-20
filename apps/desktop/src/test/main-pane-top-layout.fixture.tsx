import '../styles.css'

import { createRoot } from 'react-dom/client'

import { ShellWorkspace } from '../app/shell/top-strip'
import { group, split } from '../components/pane-shell/tree/model'
import { $hiddenTreePanes, $layoutTree, $narrowViewport } from '../components/pane-shell/tree/store'
import { registry } from '../contrib/registry'
import { MAIN_PANE_TOP_AREA } from '../sdk/areas'

// Fake pane CONTENT only: shell, tree, headers, contribution registry and CSS
// are production code. No mocked geometry or hand-copied layout styles.
for (const id of ['sessions', 'workspace', 'terminal']) {
  registry.register({ id, area: 'panes', title: id, render: () => <div data-fixture-pane={id}>{id}</div> })
}

$hiddenTreePanes.set(new Set())
$narrowViewport.set(false)
$layoutTree.set(
  split('row', [
    group(['sessions'], { id: 'sidebar' }),
    group(['workspace'], { id: 'main' }),
    group(['terminal'], { id: 'side' })
  ])
)

let dispose: (() => void) | undefined
Object.assign(window, {
  mainTopFixture: {
    contribution(mode: 'absent' | 'null' | 'visible') {
      dispose?.()
      dispose =
        mode === 'absent'
          ? undefined
          : registry.register({
              id: 'layout-proof',
              area: MAIN_PANE_TOP_AREA,
              render: () => (mode === 'null' ? null : <div style={{ height: 48 }}>Lane strip</div>)
            })
    },
    hideSidebar() {
      $hiddenTreePanes.set(new Set(['sessions']))
    }
  }
})
createRoot(document.getElementById('root')!).render(<ShellWorkspace />)
