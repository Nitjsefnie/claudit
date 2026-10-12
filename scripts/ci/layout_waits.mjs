// Readiness barriers for the rendered rules; no fixed settling sleeps.
const pendingByPage = new WeakMap();

export function trackLayoutRequests(page) {
  const pending = new Map();
  pendingByPage.set(page, pending);
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/') || ['/api/events', '/api/metrics'].includes(path)) return;
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    pending.set(request, {promise, resolve});
  });
  const finished = (request) => {
    pending.get(request)?.resolve();
    pending.delete(request);
  };
  page.on('requestfinished', finished);
  page.on('requestfailed', finished);
}

export function renderTurn(page) {
  return page.evaluate(() => new Promise((resolve) => requestAnimationFrame(
    () => requestAnimationFrame(resolve))));
}

export async function settleDashboard(page) {
  await page.waitForFunction(() => {
    const panel = document.querySelector('[data-panel]');
    if (!panel) return false;
    const box = panel.getBoundingClientRect();
    return box.width > 0 && box.height > 0
      && getComputedStyle(panel).visibility !== 'hidden';
  }, null, {timeout: 30_000});
  const pending = pendingByPage.get(page);
  let timer;
  try {
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('dashboard requests did not settle')), 30_000);
    });
    do {
      await Promise.race([Promise.all([...pending.values()].map((r) => r.promise)), deadline]);
      // Effects can start child-panel requests after the parent renders.
      await renderTurn(page);
    } while (pending.size);
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise((resolve) => requestIdleCallback(resolve));
    });
    await renderTurn(page);
  } finally {
    clearTimeout(timer);
  }
}

export async function layoutTiming(label, run) {
  if (!process.env.DEBUG_LAYOUT_TIMING) return run();
  const start = performance.now();
  try { return await run(); }
  finally {
    console.log('LAYOUT_TIMING', JSON.stringify({label, ms: performance.now() - start}));
  }
}
