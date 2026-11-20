import { expect, type Page, test } from '@playwright/test'
import { createServer, type ViteDevServer } from 'vite'

let server: ViteDevServer
let url: string

test.beforeAll(async () => {
  server = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' })
  await server.listen()
  url = `${server.resolvedUrls!.local[0]}e2e/fixtures/main-pane-top-layout.html`
})
test.afterAll(async () => {
  await server?.close()
})

async function contribution(page: Page, mode: 'absent' | 'null' | 'visible') {
  await page.evaluate(mode => {
    const fixture = (
      window as unknown as {
        mainTopFixture: { contribution(mode: string): void }
      }
    ).mainTopFixture

    fixture.contribution(mode)
  }, mode)
  // Allow React and the shell's ResizeObserver to settle, not just registry writes.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}

async function geometry(page: Page) {
  return page.evaluate(() => {
    const rect = (selector: string) => {
      const element = document.querySelector(selector)

      if (!element) {
        return null
      }
      const { x, y, width, height } = element.getBoundingClientRect()

      return { x, y, width, height }
    }

    return {
      sidebar: rect('[data-tree-group="sidebar"]'),
      side: rect('[data-tree-group="side"]'),
      main: rect('[data-tree-group="main"]'),
      header: rect('[data-tree-group="main"] [data-panel-header]'),
      body: rect('[data-fixture-pane="workspace"]'),
      strip: rect('[data-main-pane-top]'),
      windowBand: rect('[data-shell-titlebar-band]')
    }
  })
}

test('main strip consumes only main-zone space; empty/null consume none', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto(url)
  await expect(page.locator('[data-fixture-pane="terminal"]')).toBeVisible()
  const absent = await geometry(page)

  // Nonzero guards prevent hidden zones or jsdom's all-zero boxes from passing.
  for (const zone of [absent.sidebar, absent.side, absent.main]) {
    expect(zone!.height).toBe(800)
    expect(zone!.width).toBeGreaterThan(100)
    expect(zone!.y).toBe(0)
  }

  expect(absent.strip).toBeNull()
  expect(absent.windowBand).toBeNull()

  await contribution(page, 'visible')
  await expect(page.locator('[data-main-pane-top]')).toHaveText('Lane strip')
  const visible = await geometry(page)
  expect(visible.sidebar).toEqual(absent.sidebar)
  expect(visible.side).toEqual(absent.side)
  expect(visible.main).toEqual(absent.main)
  expect(visible.strip!.x).toBe(visible.main!.x)
  expect(visible.strip!.width).toBe(visible.main!.width)
  expect(visible.strip!.height).toBe(48)
  expect(visible.body!.y - absent.body!.y).toBe(48)
  expect(visible.windowBand).toBeNull()

  await contribution(page, 'null')
  const empty = await geometry(page)
  expect(empty.strip!.height).toBe(0)
  expect(empty.body).toEqual(absent.body)
  expect(empty.sidebar).toEqual(absent.sidebar)
  expect(empty.side).toEqual(absent.side)
  expect(empty.main).toEqual(absent.main)
  expect(empty.windowBand).toBeNull()
  await contribution(page, 'absent')
  expect(await geometry(page)).toEqual(absent)

  await contribution(page, 'visible')
  await page.evaluate(() =>
    (
      window as unknown as {
        mainTopFixture: { hideSidebar(): void }
      }
    ).mainTopFixture.hideSidebar()
  )
  await expect.poll(async () => (await geometry(page)).main!.x).toBe(0)
  const collapsed = await geometry(page)
  expect(collapsed.main!.height).toBe(800)
  expect(collapsed.side!.y).toBe(absent.side!.y)
  expect(collapsed.side!.height).toBe(absent.side!.height)
  expect(collapsed.header!.height).toBeGreaterThanOrEqual(34)
  expect(collapsed.strip!.y).toBeGreaterThanOrEqual(collapsed.header!.y + collapsed.header!.height)
  expect(collapsed.strip!.x).toBe(0)
  expect(collapsed.strip!.width).toBe(collapsed.main!.width)
  expect(collapsed.windowBand).toBeNull()
  await testInfo.attach('main-top-geometry', {
    body: JSON.stringify({ absent, visible, empty, collapsed }, null, 2),
    contentType: 'application/json'
  })
})
