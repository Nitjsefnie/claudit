// Readiness barriers for the rendered rules; no fixed settling sleeps.
const trackedByPage = new WeakMap();

// CDP tags each request with its document's loader: a replaced document's
// requests never finish, so a settle waits only on the current document's.
export function trackLayoutRequests(page) {
  return withDeadline(() => trackOverCdp(page), 'layout request tracking');
}

async function trackOverCdp(page) {
  const cdp = await page.context().newCDPSession(page);
  const tracked = {cdp, pending: new Map(), loaderId: null};
  trackedByPage.set(page, tracked);
  cdp.on('Network.requestWillBeSent', ({requestId, loaderId, request}) => {
    const path = new URL(request.url).pathname;
    if (!path.startsWith('/api/') || ['/api/events', '/api/metrics'].includes(path)
      || tracked.pending.has(requestId)) return;
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    tracked.pending.set(requestId, {url: request.url, loaderId, promise, resolve});
  });
  const finished = ({requestId}) => {
    tracked.pending.get(requestId)?.resolve();
    tracked.pending.delete(requestId);
  };
  cdp.on('Network.loadingFinished', finished);
  cdp.on('Network.loadingFailed', finished);
  // A main-frame commit releases the old document's requests.
  cdp.on('Page.frameNavigated', ({frame}) => {
    if (frame.parentId) return;
    tracked.loaderId = frame.loaderId;
    for (const [requestId, r] of tracked.pending) if (r.loaderId !== frame.loaderId) finished({requestId});
  });
  await Promise.all([cdp.send('Network.enable'), cdp.send('Page.enable')]);
}

// A function label is read at expiry; one that throws still rejects.
export async function withDeadline(run, label, timeoutMs = 30_000) {
  const expired = () => {
    try {
      return new Error(`timed out waiting for ${typeof label === 'function' ? label() : label}`);
    } catch (error) {
      return new Error(`timed out waiting; its label failed: ${error.message}`, {cause: error});
    }
  };
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

export async function settleDashboard(page, options = {}) {
  if (Object(options) !== options) throw new TypeError(`settleDashboard takes {timeoutMs, call}, not ${options}`);
  const {timeoutMs = 30_000, call = ''} = options;
  const tracked = trackedByPage.get(page);
  if (!tracked) throw new Error('settleDashboard needs trackLayoutRequests(page) first');
  // Every stage spends the same end-to-end budget, including panel startup.
  const expires = performance.now() + timeoutMs;
  const current = () => [...tracked.pending.values()].filter((r) => r.loaderId === tracked.loaderId);
  const state = (label) => `dashboard readiness at ${[`width ${page.viewportSize()?.width}`, call]
    .filter(Boolean).join(', ')}: ${label}; pending API requests: ${
    current().map((r) => r.url).join(', ') || 'none'}`;
  const wait = (label, run) => withDeadline(run, () => state(label), expires - performance.now());
  // Read: this session may not have seen the commit event yet.
  await wait('current document', async () => {
    tracked.loaderId = (await tracked.cdp.send('Page.getFrameTree')).frameTree.frame.loaderId;
  });
  await wait('visible panel', () => page.waitForFunction(() => {
    const panel = document.querySelector('[data-panel]');
    if (!panel) return false;
    const box = panel.getBoundingClientRect();
    return box.width > 0 && box.height > 0
      && getComputedStyle(panel).visibility !== 'hidden';
  }, null, {timeout: timeoutMs}));
  do {
    await wait('API requests', () => Promise.all(current().map((r) => r.promise)));
    // Effects can start child-panel requests after the parent renders.
    await wait('request render frames', () => page.evaluate(renderFrames));
  } while (current().length);
  await wait('fonts and idle callback', () => page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolve) => requestIdleCallback(resolve));
  }));
  await wait('final render frames', () => page.evaluate(renderFrames));
}

export async function layoutTiming(label, run) {
  if (!process.env.CLAUDIT_DEBUG_LAYOUT_TIMING) return run();
  const start = performance.now();
  try { return await run(); }
  finally {
    console.log('LAYOUT_TIMING', JSON.stringify({label, ms: performance.now() - start}));
  }
}
