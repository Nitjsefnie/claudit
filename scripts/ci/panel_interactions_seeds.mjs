// The #690 seeded-violation proof: each of the three Expected-Behavior
// bullets needs a violation of its category planted into an otherwise
// clean run, and the sweep must FAIL on it. This runner drives
// panel_interactions.mjs once per seed with PANEL_INTERACTIONS_SEED set,
// asserts exit 1 per seed, and greps the sweep's output for the finding
// kind each seed exists to prove, so a seed that fails for the WRONG
// reason (the page broke, the ledger fired, the fixtures vanished) does
// not read as the proof passing.
//
// #843: the seeded runs used to re-drive the FULL sweep, one after
// another — half the leg's wall time for a proof of three classifiers.
// The three seeds now run concurrently, each at ONE viewport width (the
// seeds prove the classifiers, not the sweep's reach) and without a
// target cap, so each seeded run exercises the real default config. The
// sweep skips its height pass under a seed for the same reason.
//
// Runs in panel-layout.yml right after the sweep step. Needs no
// database and no R2 — the same real page, the same fixtures.
import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SWEEP = join(HERE, 'panel_interactions.mjs');

// Seed -> the finding kind its run must name in its output.
const SEED_KINDS = {
  'no-targets': 'no-targets',
  'other-region': 'other-region',
  'cold-region': 'other-region',
};

// One seeded run: the sweep process with the seed set, resolved with
// its exit code and the output the kind grep reads.
const runSeed = ([seed, kind]) => new Promise(resolve => {
  const child = spawn(process.execPath, [SWEEP], {
    env: { ...process.env, PANEL_INTERACTIONS_SEED: seed,
      // One width: the seeds prove the CLASSIFIER, not the sweep's
      // reach, so three of the four renders add cost and prove nothing.
      PANEL_LAYOUT_WIDTHS: '1440' },
  });
  let out = '';
  child.stdout.on('data', d => { out += d; });
  child.stderr.on('data', d => { out += d; });
  child.on('close', code => resolve({ seed, kind, code, out }));
});

const results = await Promise.all(
  Object.entries(SEED_KINDS).map(runSeed));

let failed = 0;
for (const { seed, kind, code, out } of results) {
  const failedOnTheSeed = code === 1 && out.includes(kind);
  if (!failedOnTheSeed) {
    failed += 1;
    console.error(`SEED ${seed}: expected exit 1 naming ${kind}, got `
      + `exit ${code}${code === null ? ' (killed by a signal)' : ''}`
      + (out.includes(kind) ? '' : ' without naming the kind')
      + `\n--- sweep output ---\n${out.slice(-4000)}`);
  } else {
    console.log(`SEED ${seed}: exit 1 naming ${kind} — proven red`);
  }
}
process.exit(failed ? 1 : 0);
