// The static server the rendered dashboard guards share (issue #647).
//
// Extracted verbatim from scripts/ci/panel_layout.mjs so the interaction
// sweep can serve the same REAL page — the same public/index.html, the
// same src/*.jsx, the same in-browser Babel — against the same frozen
// fixture dataset. Both guards read one copy of the /api path table, so
// an endpoint the frontend starts fetching cannot be added to one guard
// and forgotten in the other.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

export const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
export const FIXTURES = join(REPO, 'fixtures', 'layout');

// The viewport widths both guards drive. Four: a desktop the panels were
// designed at, a laptop where the grid reflows, and two phone widths
// where every label is longest relative to the space it has. 320 is the
// viewport issue #630 was reported at, where the panel's title ran off
// the right edge and the legend's wrapped row fell off the bottom.
// Override locally with PANEL_LAYOUT_WIDTHS=1440,375.
export const WIDTHS = (process.env.PANEL_LAYOUT_WIDTHS || '1440,1024,375,320')
  .split(',').map(Number).filter(n => n > 0);

// Every /api path the frontend fetches, and the fixture that answers it.
export const API = {
  '/api/dashboard': 'dashboard.json',
  '/api/context-growth/traces': 'ctx_traces.json',
  '/api/projects': 'projects.json',
  '/api/models': 'models.json',
  '/api/me': 'me.json',
  '/api/tool-usage': 'tool_usage.json',
  '/api/tool-error-rate': 'tool_error_rate.json',
  '/api/reply-latency': 'reply_latency.json',
  '/api/activity-heatmap': 'activity_heatmap.json',
  '/api/cost-by-context': 'cost_by_context.json',
  '/api/cost-by-agent': 'cost_by_agent.json',
  '/api/web-metrics': 'web_metrics.json',
};

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.jsx': 'text/babel; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.ico': 'image/x-icon',
};

export function serve() {
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const rel = decodeURIComponent(url.pathname);
    let file;
    if (rel === '/' || rel === '/index.html') {
      // The backend injects this line per request; the guards supply the
      // same globals so the very first React render already knows them.
      const html = (await readFile(join(REPO, 'public', 'index.html'), 'utf8'))
        .replace(
          "<script>window.BACKEND_URL = window.BACKEND_URL || '';</script>",
          "<script>window.BACKEND_URL = '/'; window.IS_GUEST = false; "
          + 'window.IS_OPERATOR = true; window.BRAND = '
          + '{"name":"claudit","title":"claudit","description":"guard"};'
          + '</script>');
      res.writeHead(200, { 'content-type': TYPES['.html'] });
      res.end(html);
      return;
    }
    if (rel.startsWith('/src/')) file = join(REPO, normalize(rel));
    else if (rel === '/app.css' || rel === '/favicon.ico') {
      file = join(REPO, 'public', rel.slice(1));
    } else {
      res.writeHead(404).end();
      return;
    }
    // The repository root is the only tree this server reads from.
    if (!file.startsWith(REPO) || !existsSync(file)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, {
      'content-type': TYPES[rel.slice(rel.lastIndexOf('.'))]
        || 'application/octet-stream',
    });
    res.end(await readFile(file));
  });
  return new Promise(resolve => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}
