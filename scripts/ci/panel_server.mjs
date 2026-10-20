// The static server the rendered dashboard guards share.
//
// Shared so the layout and interaction guards serve the same REAL page:
// the same public/index.html, the same src/*.jsx, the same in-browser Babel — against the same frozen
// fixture dataset. Both guards read one copy of the /api path table, so
// an endpoint the frontend starts fetching cannot be added to one guard
// and forgotten in the other.
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { dirname, extname, join, sep } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

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

const MINIFY_SOURCES = `
from pathlib import Path
import sys
from backend.js_min import minify_js

source = Path('src')
output = Path(sys.argv[1])
for path in source.rglob('*'):
    if path.suffix in {'.js', '.jsx'} and path.is_file():
        relative = path.relative_to(source)
        parent = output
        for part in relative.parts[:-1]:
            parent = parent / part
            parent.mkdir(exist_ok=True)
        target = output / relative
        target.write_bytes(minify_js(path.read_bytes().decode('utf-8')).encode('utf-8'))
`;

export async function serve() {
  const transformed = mkdtempSync(join(tmpdir(), 'claudit-panel-src-'));
  let child;
  const cleanup = () => {
    child?.kill('SIGKILL');
    rmSync(transformed, {recursive: true, force: true, maxRetries: 3, retryDelay: 10});
  };
  const terminate = () => {
    if (child) {
      child.once('close', () => process.exit(143));
      child.kill('SIGKILL');
    } else process.exit(143);
  };
  const release = () => {
    cleanup();
    process.off('exit', cleanup);
    process.off('SIGTERM', terminate);
  };
  // Own the directory before startup can yield; exit handlers are synchronous.
  // The child creates descendants only, so it cannot recreate a deleted root.
  process.once('exit', cleanup);
  process.once('SIGTERM', terminate);
  try {
    if (transformed.startsWith(REPO + sep)) {
      throw new Error('Sweep temporary sources must be outside the repository');
    }
    await new Promise((resolve, reject) => {
      child = execFile('python3', ['-c', MINIFY_SOURCES, transformed], {cwd: REPO},
        (error, stdout, stderr) => {
          child = null;
          if (error) reject(Object.assign(error, {stdout, stderr}));
          else resolve();
        });
    });
  } catch (error) {
    release();
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
  server.once('close', release);
  return new Promise((resolve, reject) => {
    server.once('error', error => {
      release();
      reject(error);
    });
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}
