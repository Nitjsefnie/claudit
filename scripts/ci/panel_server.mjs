// The static server the rendered dashboard guards share.
//
// Shared so the layout and interaction guards serve the same REAL page:
// the same public/index.html, the same src/*.jsx, the same in-browser Babel — against the same frozen
// fixture dataset. Both guards read one copy of the /api path table, so
// an endpoint the frontend starts fetching cannot be added to one guard
// and forgotten in the other.
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile } from 'node:fs/promises';
import { existsSync, rmSync } from 'node:fs';
import { dirname, extname, join, sep } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

export const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
export const FIXTURES = join(REPO, 'fixtures', 'layout');

// The viewport widths both guards drive: a desktop, a laptop where
// the grid reflows, and two phone widths where every label is longest
// relative to the space it has. The narrowest width checks that titles
// and wrapped legends stay inside their panels.
// Override locally with CLAUDIT_PANEL_LAYOUT_WIDTHS=1440,375.
export const WIDTHS = (process.env.CLAUDIT_PANEL_LAYOUT_WIDTHS || '1440,1024,375,320')
  .split(',').map(Number).filter(n => n > 0);

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

const minify = promisify(execFile);
const MINIFY_SOURCES = `
from pathlib import Path
import sys
from backend.js_min import minify_js

source = Path('src')
output = Path(sys.argv[1])
for path in source.rglob('*'):
    if path.suffix in {'.js', '.jsx'} and path.is_file():
        target = output / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(minify_js(path.read_bytes().decode('utf-8')).encode('utf-8'))
`;

export async function serve() {
  const transformed = await mkdtemp(join(tmpdir(), 'claudit-panel-src-'));
  const cleanup = () => rmSync(transformed, {recursive: true, force: true});
  try {
    if (transformed.startsWith(REPO + sep)) {
      throw new Error('Sweep temporary sources must be outside the repository');
    }
    await minify('python3', ['-c', MINIFY_SOURCES, transformed], {cwd: REPO});
  } catch (error) {
    cleanup();
    throw error;
  }
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const rel = decodeURIComponent(url.pathname);
    let file, root;
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
    if (rel.startsWith('/src/')) {
      root = ['.js', '.jsx'].includes(extname(rel)) ? transformed : join(REPO, 'src');
      file = join(root, rel.slice('/src/'.length));
    } else if (rel.startsWith('/vendor/')) {
      root = join(REPO, 'public', 'vendor');
      file = join(root, rel.slice('/vendor/'.length));
    } else if (rel === '/app.css' || rel === '/favicon.ico') {
      root = join(REPO, 'public');
      file = join(root, rel.slice(1));
    } else {
      res.writeHead(404).end();
      return;
    }
    if (!file.startsWith(root + sep) || !existsSync(file)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, {
      'content-type': TYPES[rel.slice(rel.lastIndexOf('.'))]
        || 'application/octet-stream',
    });
    res.end(await readFile(file));
  });
  // Some sweeps exit immediately after close(); reap on either lifecycle path.
  process.once('exit', cleanup);
  server.once('close', () => {
    process.off('exit', cleanup);
    cleanup();
  });
  return new Promise((resolve, reject) => {
    server.once('error', error => {
      process.off('exit', cleanup);
      cleanup();
      reject(error);
    });
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}
