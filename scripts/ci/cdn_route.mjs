import { readFileSync } from 'node:fs';
import { setTimeout } from 'node:timers/promises';

const html = readFileSync(new URL('../../public/index.html', import.meta.url), 'utf8');
const urls = [...html.matchAll(/<script\b[^>]*\bsrc="(https:\/\/unpkg\.com\/[^\"]+)"/g)]
  .map(match => match[1]);
const scripts = new Map();
const ATTEMPTS = 3;
const TIMEOUT_MS = 8_000;

async function fetchScript(url, fetchImpl, wait) {
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    let response, body;
    try {
      response = await fetchImpl(url, {signal: AbortSignal.timeout(TIMEOUT_MS)});
      if (response.ok) body = Buffer.from(await response.arrayBuffer());
    } catch (error) {
      if (attempt === ATTEMPTS) {
        const last = error.cause ? `${error.message}: ${error.cause.message}` : error.message;
        throw new Error(`CDN script ${url} failed after ${attempt} attempts: ${last}`, {cause: error});
      }
      await wait(250 * attempt);
      continue;
    }
    if (!response.ok) {
      throw new Error(`CDN script ${url}: HTTP ${response.status} ${response.statusText}`);
    }
    // fetch decodes the body; forward bytes without compressed length/encoding headers.
    return {status: response.status, body, headers: {
      'content-type': response.headers.get('content-type') || 'text/javascript',
      'access-control-allow-origin': '*',
    }};
  }
}

export async function createSweepContext(browser, options = {}, {
  fetchImpl = globalThis.fetch, wait = setTimeout,
} = {}) {
  // Local scripts need no recovery: the sweep server supplies them directly.
  // Prefetch before allocating a context so exhaustion rejects setup, outside a route callback.
  const responses = await Promise.all(urls.map(url => {
    if (!scripts.has(url)) scripts.set(url, fetchScript(url, fetchImpl, wait));
    return scripts.get(url);
  }));
  const context = await browser.newContext(options);
  for (const [i, url] of urls.entries()) {
    await context.route(url, route => route.fulfill(responses[i]));
  }
  return context;
}
