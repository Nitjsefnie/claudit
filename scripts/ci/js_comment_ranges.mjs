import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

try {
  const require = createRequire(import.meta.url);
  const espree = require(path.join(process.argv[2], 'espree'));
  const files = JSON.parse(readFileSync(0, 'utf8'));
  const comments = files.map((file) => {
    try {
      const source = readFileSync(file, 'utf8');
      const parsed = espree.parse(source, {
        ecmaVersion: 'latest',
        sourceType: 'module',
        comment: true,
        range: true,
        ecmaFeatures: { jsx: file.endsWith('.jsx') },
      });
      return parsed.comments.map(({ range }) => source.slice(...range));
    } catch (error) {
      throw new Error(`${file}: ${error.message}`);
    }
  });
  process.stdout.write(JSON.stringify(comments));
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
}
