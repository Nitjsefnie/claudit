// The #690 seeded-violation proof: each of the three Expected-Behavior
// bullets needs a violation of its category planted into an otherwise
// clean run, and the sweep must FAIL on it. This runner drives
// panel_interactions.mjs once per seed with PANEL_INTERACTIONS_SEED set,
// asserts exit 1 per seed, and greps the sweep's output for the finding
// kind each seed exists to prove, so a seed that fails for the WRONG
// reason (the page broke, the ledger fired, the fixtures vanished) does
// not read as the proof passing.
//
// Runs in panel-layout.yml right after the sweep step. Needs no
// database and no R2 — the same real page, the same fixtures.
import { spawnSync } from 'node:child_process';
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

let failed = 0;
for (const [seed, kind] of Object.entries(SEED_KINDS)) {
  const r = spawnSync(process.execPath, [SWEEP], {
    env: { ...process.env, PANEL_INTERACTIONS_SEED: seed,
      // The seeds prove the CLASSIFIER, not the sweep's reach: a bound
      // of 8 targets per panel keeps each seeded run short without
      // touching what the seed plants or what must fire.
      PANEL_INTERACTIONS_MAX_TARGETS: '8' },
    encoding: 'utf8',
  });
  const out = (r.stdout || '') + (r.stderr || '');
  const failedOnTheSeed = r.status === 1 && out.includes(kind);
  if (!failedOnTheSeed) {
    failed += 1;
    console.error(`SEED ${seed}: expected exit 1 naming ${kind}, got `
      + `exit ${r.status}${r.signal ? ` (${r.signal})` : ''}`
      + (out.includes(kind) ? '' : ' without naming the kind')
      + `\n--- sweep output ---\n${out.slice(-4000)}`);
  } else {
    console.log(`SEED ${seed}: exit 1 naming ${kind} — proven red`);
  }
}
process.exit(failed ? 1 : 0);
