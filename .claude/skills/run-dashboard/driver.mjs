#!/usr/bin/env node
// Drives the running CrossFlow AI dashboard with headless Chromium and
// screenshots every tab plus the solver result and pitch deck. Playwright
// is not a project dependency (see SKILL.md), so this resolves it from the
// npx cache at runtime rather than assuming a local node_modules/playwright.
//
// Usage:
//   node driver.mjs <outdir> [scenario]
//   scenario: all (default) | map | route | ferry | analytics | pitch
//
// Exit code is non-zero if the page threw a console/page error during the
// run. Screenshots are named <scenario>.png inside <outdir>.

import { existsSync } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { globSync } from 'node:fs';

const BASE_URL = process.env.CROSSFLOW_URL || 'http://localhost:3000';
const outdir = process.argv[2];
const scenario = process.argv[3] || 'all';

if (!outdir) {
  console.error('usage: node driver.mjs <outdir> [all|map|route|ferry|analytics|pitch]');
  process.exit(2);
}

function findPlaywright() {
  // A bare `import 'playwright'` resolves relative to this script's own
  // directory (which has no node_modules), not the caller's cwd — so it
  // fails even when playwright is installed elsewhere. Resolve an absolute
  // path to the npx cache copy instead.
  const candidates = globSync(join(homedir(), '.npm/_npx/*/node_modules/playwright/index.mjs'));
  if (candidates.length > 0) return candidates[0];
  throw new Error(
    "playwright not found under ~/.npm/_npx/*/node_modules/playwright.\n" +
    "Install it once with: npx --yes playwright@latest --version\n" +
    "Then chromium with: npx --yes playwright install chromium"
  );
}

const { chromium } = await import(findPlaywright());

await mkdir(outdir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });

const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));

const shots = [];
async function shot(name) {
  const path = join(outdir, `${name}.png`);
  await page.screenshot({ path });
  shots.push(path);
}

async function gotoTab(label) {
  await page.getByRole('button', { name: label }).click();
  await page.waitForTimeout(600);
}

console.log(`→ ${BASE_URL}`);
await page.goto(BASE_URL, { waitUntil: 'networkidle' });
await page.waitForTimeout(3500); // leaflet tiles + first poll + route geometry

const badge = await page.locator('.badge').first().textContent().catch(() => null);
console.log(`badge: ${badge}`);

if (scenario === 'all' || scenario === 'map') {
  await shot('map');
}

if (scenario === 'all' || scenario === 'route') {
  await gotoTab('Smart Route & Departure Solver');
  await page.getByRole('button', { name: 'Compute AI Route Recommendation' }).click();
  await page.waitForTimeout(2200); // ML solver call + render
  await shot('route');
}

if (scenario === 'all' || scenario === 'ferry') {
  await gotoTab('Ferry & Port Intelligence');
  await shot('ferry');
}

if (scenario === 'all' || scenario === 'analytics') {
  await gotoTab('Operations & Carbon Analytics');
  await shot('analytics');
}

if (scenario === 'all' || scenario === 'pitch') {
  await page.getByRole('button', { name: 'Stage Pitch Deck' }).click();
  await page.waitForTimeout(600);
  await shot('pitch');
  await page.keyboard.press('Escape').catch(() => {});
}

await browser.close();

console.log('screenshots:', shots.join(', '));
console.log('console errors:', errors.length ? errors : 'none');
process.exit(errors.length ? 1 : 0);
