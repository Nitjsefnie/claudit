import { readFileSync, readdirSync, mkdirSync, writeFileSync } from 'node:fs';
import { isAbsolute, resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { parse } from 'acorn';
import { convert } from 'ast-v8-to-istanbul';
import libCoverage from 'istanbul-lib-coverage';
import v8Coverage from '@bcoe/v8-coverage';
import TestExclude from 'test-exclude';

function scriptPath(url) {
  try {
    const filename = url.startsWith('file://') ? fileURLToPath(url) : url;
    return isAbsolute(filename) ? resolve(filename) : null;
  } catch {
    return null;
  }
}

function captures(directory, exclude) {
  const reports = [];
  for (const name of readdirSync(directory).sort()) {
    let report;
    try {
      report = JSON.parse(readFileSync(join(directory, name), 'utf8'));
    } catch {
      continue;
    }
    if (!Array.isArray(report?.result)) continue;
    const result = [];
    for (const script of report.result) {
      const filename = scriptPath(script.url);
      if (filename && exclude.shouldInstrument(filename)) {
        result.push({ ...script, url: filename });
      }
    }
    reports.push({ result });
  }
  return v8Coverage.mergeProcessCovs(reports).result;
}

async function main() {
  const { values } = parseArgs({
    options: {
      'temp-directory': { type: 'string', default: process.env.NODE_V8_COVERAGE },
      'reports-dir': { type: 'string', default: 'coverage' },
      include: { type: 'string', default: 'src/**/*.js' },
      lines: { type: 'string' },
    },
  });
  if (!values['temp-directory']) throw new Error('A V8 coverage temp directory is required');
  const floor = values.lines === undefined ? undefined : Number(values.lines);
  if (floor !== undefined && (!Number.isFinite(floor) || floor < 0 || floor > 100)) {
    throw new Error('The line coverage floor must be between 0 and 100');
  }
  // Keep c8's default exclusions and executed-files-only population.
  const exclude = new TestExclude({
    cwd: process.cwd(), include: [values.include],
    relativePath: true, excludeNodeModules: true,
  });
  const map = libCoverage.createCoverageMap();
  for (const script of captures(values['temp-directory'], exclude)) {
    const code = readFileSync(script.url, 'utf8');
    map.merge(await convert({
      code,
      ast: parse(code, { ecmaVersion: 'latest', sourceType: 'module', locations: true }),
      coverage: { ...script, url: pathToFileURL(script.url).href },
    }));
  }
  const summary = { total: map.getCoverageSummary().toJSON() };
  for (const file of map.files().sort()) {
    summary[file] = map.fileCoverageFor(file).toSummary().toJSON();
  }
  mkdirSync(values['reports-dir'], { recursive: true });
  writeFileSync(join(values['reports-dir'], 'coverage-summary.json'), JSON.stringify(summary) + '\n');
  if (floor !== undefined && summary.total.lines.pct < floor) {
    console.error(`Line coverage ${summary.total.lines.pct}% is below ${floor}%`);
    process.exitCode = 1;
  }
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
