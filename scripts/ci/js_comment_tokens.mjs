// Print the comment-stripped espree token stream of one JS/JSX source
// file as JSON. The ci-gate classifier's comment-only class compares two
// of these streams: equal means the change carried only comments, so the
// behaviour legs cannot have moved.
//
// Each token prints [type, value, brokeLine]: the third field is true
// when a line terminator sits between this token and the previous one.
// It MUST be there. A line terminator is semantic in JS — the ASI
// restricted productions (`return`/`throw`/`break`/`continue`/postfix
// `++`/`--`) terminate on a newline, and espree's tokens otherwise erase
// whitespace entirely, so a change that only moves a line break could
// read as comment-only while it changes behaviour. Python needs no such
// field: its NEWLINE/INDENT/DEDENT tokens already carry the line
// structure, and only COMMENT/NL are dropped.
//
// No analogue of Python's load-bearing magic comments is needed: espree
// drops a JS hashbang (`#!...`) entirely, and it is non-semantic — it
// names an interpreter for direct OS execution, never affects the
// program the engine evaluates, and no ci-gate leg runs a repo JS file
// by its shebang — so a hashbang-only change cannot move any leg.
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
    loc: true,
    ecmaFeatures: { jsx },
  });
  process.stdout.write(JSON.stringify(tokens.map((t, i, a) => [
    t.type, t.value, i > 0 && t.loc.start.line > a[i - 1].loc.end.line,
  ])));
} catch (err) {
  process.stderr.write(String((err && err.message) || err));
  process.exit(1);
}
