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

export async function withDeadline(run, label, timeoutMs = 30_000) {
  const expired = () => new Error(`timed out waiting for ${label}`);
  if (!(timeoutMs > 0)) throw expired();
  let timer;
  try {
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(expired()), timeoutMs);
    });
    return await Promise.race([Promise.resolve().then(run), deadline]);
  } finally {
    clearTimeout(timer);
  }
}

const renderFrames = () => new Promise((resolve) => requestAnimationFrame(
  () => requestAnimationFrame(resolve)));

export function renderTurn(page, timeoutMs = 30_000) {
  return withDeadline(() => page.evaluate(renderFrames), 'render frames', timeoutMs);
}

export async function settleDashboard(page, timeoutMs = 30_000) {
  // Every stage spends the same end-to-end budget, including panel startup.
  const expires = performance.now() + timeoutMs;
  const wait = (label, run) => withDeadline(run, `dashboard readiness: ${label}`,
    expires - performance.now());
  await wait('visible panel', () => page.waitForFunction(() => {
    const panel = document.querySelector('[data-panel]');
    if (!panel) return false;
    const box = panel.getBoundingClientRect();
    return box.width > 0 && box.height > 0
      && getComputedStyle(panel).visibility !== 'hidden';
  }, null, {timeout: timeoutMs}));
  const pending = pendingByPage.get(page);
  do {
    await wait('API requests', () => Promise.all([...pending.values()].map((r) => r.promise)));
    // Effects can start child-panel requests after the parent renders.
    await wait('request render frames', () => page.evaluate(renderFrames));
  } while (pending.size);
  await wait('fonts and idle callback', () => page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolve) => requestIdleCallback(resolve));
  }));
  await wait('final render frames', () => page.evaluate(renderFrames));
}

export async function layoutTiming(label, run) {
  if (!process.env.DEBUG_LAYOUT_TIMING) return run();
  const start = performance.now();
  try { return await run(); }
  finally {
    console.log('LAYOUT_TIMING', JSON.stringify({label, ms: performance.now() - start}));
  }
}
