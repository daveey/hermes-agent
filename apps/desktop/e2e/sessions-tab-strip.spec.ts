import { readFileSync } from 'node:fs'

import { expect, test } from '@playwright/test'
import { compile } from 'tailwindcss'

// Compile the production slot's actual utilities: a duplicated CSS fixture could
// stay green while the component regresses to unsafe end alignment.
const source = readFileSync(new URL('../src/components/pane-shell/tree/renderer/sessions-tab-strip.tsx', import.meta.url), 'utf8')
const classes = /className="([^"]+)"/.exec(source)![1]

test('overflowing Sessions contributions remain reachable from first to last', async ({ page }) => {
  const compiler = await compile('@tailwind utilities;')
  const css = compiler.build(classes.split(' '))
  await page.setContent(`<style>${css}
    * { box-sizing: border-box; }
    #strip { display: flex; width: 300px; }
    #tabs { flex: 1 1 0%; min-width: 0; }
    button { flex: 0 0 150px; }
  </style><div id="strip"><div id="tabs">Sessions / Bots</div>
    <div id="slot" class="${classes}"><button>First</button><button>Last</button></div>
  </div>`)
  const slot = page.locator('#slot')

  const geometry = await slot.evaluate(element => {
    const first = element.firstElementChild!

    return {
      width: element.clientWidth,
      scrollWidth: element.scrollWidth,
      firstOffset: first.getBoundingClientRect().left - element.getBoundingClientRect().left
    }
  })

  expect(geometry.width).toBe(195)
  expect(geometry.scrollWidth).toBe(300)
  expect(geometry.firstOffset).toBe(0)
  await expect(page.getByRole('button', { name: 'First' })).toBeInViewport({ ratio: 1 })
  await slot.evaluate(element => { element.scrollLeft = element.scrollWidth })
  await expect(page.getByRole('button', { name: 'Last' })).toBeInViewport({ ratio: 1 })
  expect(await slot.evaluate(element => element.scrollLeft)).toBe(105)
})
