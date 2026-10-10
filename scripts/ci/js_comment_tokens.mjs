// Print the comment-stripped espree token stream of one JS/JSX source
// file as JSON. The ci-gate classifier's comment-only class compares two
// of these streams: equal means the change carried only comments, so the
// behaviour legs cannot have moved.
//
// Usage:
//   node js_comment_tokens.mjs <espree-module-dir> <source-file> <jsx|plain>
//
// espree is resolved from the given module dir explicitly, so it need not
// be in the repository's node_modules; the caller passes the dir it
// installed espree into.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const espreeDir = process.argv[2];
const sourceFile = process.argv[3];
const jsx = process.argv[4] === 'jsx';

try {
  const require = createRequire(import.meta.url);
  const espree = require(path.join(espreeDir, 'espree'));
  const src = readFileSync(sourceFile, 'utf8');
  const tokens = espree.tokenize(src, {
    ecmaVersion: 'latest',
    sourceType: 'module',
    comment: false,
    ecmaFeatures: { jsx },
  });
  process.stdout.write(JSON.stringify(tokens.map(t => [t.type, t.value])));
} catch (err) {
  process.stderr.write(String((err && err.message) || err));
  process.exit(1);
}
