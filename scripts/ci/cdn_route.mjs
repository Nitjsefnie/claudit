import { readFileSync } from 'node:fs';
import { setTimeout } from 'node:timers/promises';

const html = readFileSync(new URL('../../public/index.html', import.meta.url), 'utf8');
const urls = [...html.matchAll(/<script\b[^>]*\bsrc="(https:\/\/unpkg\.com\/[^\"]+)"/g)]
  .map(match => match[1]);
const fontCss = html.match(/<link\b[^>]*\bhref="(https:\/\/fonts\.googleapis\.com\/css2\?[^"]+)"/)?.[1];
const preloads = [...html.matchAll(/<link\b[^>]*\bhref="(https:\/\/fonts\.gstatic\.com\/s\/[^"]+)"/g)]
  .map(match => match[1]);
export const fontFamilies = fontCss
  ? new URL(fontCss).searchParams.getAll('family').map(family => family.split(':')[0]) : [];
// Google serves woff2 faces only to a browser; Chromium then loads exactly these.
const BROWSER_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';
const assets = new Map();
const ATTEMPTS = 3;
const TIMEOUT_MS = 8_000;

async function fetchAsset(url, fetchImpl, wait, headers) {
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    let response, body;
    try {
      response = await fetchImpl(url, {headers, signal: AbortSignal.timeout(TIMEOUT_MS)});
      if (response.ok) body = Buffer.from(await response.arrayBuffer());
    } catch (error) {
      if (attempt === ATTEMPTS) {
        const last = error.cause ? `${error.message}: ${error.cause.message}` : error.message;
        throw new Error(`CDN asset ${url} failed after ${attempt} attempts: ${last}`, {cause: error});
      }
      await wait(250 * attempt);
      continue;
    }
    if (!response.ok) {
      throw new Error(`CDN asset ${url}: HTTP ${response.status} ${response.statusText}`);
    }
    // fetch decodes the body; forward bytes without compressed length/encoding headers.
    return {status: response.status, body, headers: {
      'content-type': response.headers.get('content-type') || 'text/javascript',
      'access-control-allow-origin': '*',
    }};
  }
}

// One fetch per URL per run: every context, both worlds alike, gets these bytes.
function fetchOnce(url, fetchImpl, wait, headers = {}) {
  if (!assets.has(url)) assets.set(url, fetchAsset(url, fetchImpl, wait, headers));
  return assets.get(url);
}

async function fetchFonts(fetchImpl, wait) {
  if (!fontCss) return new Map();
  const css = await fetchOnce(fontCss, fetchImpl, wait, {'user-agent': BROWSER_UA});
  const faces = [...new Set([...preloads, ...[...css.body.toString('utf8')
    .matchAll(/url\((https:\/\/fonts\.gstatic\.com\/[^)]+)\)/g)].map(match => match[1])])];
  const bodies = await Promise.all(faces.map(url => fetchOnce(url, fetchImpl, wait)));
  return new Map([[fontCss, css], ...faces.map((url, i) => [url, bodies[i]])]
    .map(([url, response]) => [new URL(url).href, response]));
}

export async function createSweepContext(browser, options = {}, {
  fetchImpl = globalThis.fetch, wait = setTimeout,
} = {}) {
  // Local scripts need no recovery: the sweep server supplies them directly.
  // Prefetch before allocating a context so exhaustion rejects setup, outside a route callback.
  const [responses, fonts] = await Promise.all([
    Promise.all(urls.map(url => fetchOnce(url, fetchImpl, wait))), fetchFonts(fetchImpl, wait)]);
  const context = await browser.newContext(options);
  for (const [i, url] of urls.entries()) {
    await context.route(url, route => route.fulfill(responses[i]));
  }
  const hosts = new Set([...fonts.keys()].map(url => new URL(url).host));
  // A face the served stylesheet never named aborts alike in every context.
  if (fonts.size) {
    await context.route(url => hosts.has(url.host), route => {
      const hit = fonts.get(route.request().url());
      return hit ? route.fulfill(hit) : route.abort();
    });
  }
  return context;
}
