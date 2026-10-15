#!/usr/bin/env python3
"""Seed and tighten the recorded suite cost budgets.

Each suite phase carries a cost in millions of instructions. Its
``floor = measured + 1.5`` is the ceiling at the recorded seed tree,
whose size is ``L_seed = tests_tree_lines``. At a run tree of L lines,
the ceiling is ``floor + per_line * max(0, L - L_seed) / 1e6``.
Optional ``per_line`` is a non-negative integer in instructions per
tests-tree line; absent or zero means an absolute ceiling. Ordinary
growth at that slope is budgeted. The 1.5M gap covers interpreter and
library drift; the workflows pin the interpreter micro version.

Tightening only lowers cost fields. For an artifact count c at L >=
L_seed, subtract the budgeted growth to get the exact seed-equivalent
``c_norm``. A phase qualifies when ``recorded_measured - c_norm > 1.5``.
Store c_norm rounded UP to one decimal as measured, and measured + 1.5
as floor. The comparison uses the unrounded c_norm. A negative c_norm
skips that phase and reports that growth exceeds the reading, a seed
candidate. Smaller-tree artifacts never tighten, and an artifact with
no identity is refused when a budget exists. Slopes and the recorded
identity stay fixed; no eligible phase means no file write.

Seeding requires TWO gateable runner artifacts, both counted under
``PYTHONHASHSEED=0`` with positive identities: m1 measures the final
tree the seed lands on, and m2 measures the same code plus a larger
probe tree (L2 > L1). The seed records m1's counts and identity and
derives each phase's per_line as
``max(0, ceil((c2 - c1) * 1e6 / (L2 - L1)))``. Decimal arithmetic
converts the one-decimal million counts to instructions before dividing.
The CLI requires ``--seed m1.json --growth m2.json`` and checks m1's
identity against the checkout. Both direct and CLI seeds refuse an
already-recorded family and write through the strict loader.

A changed fixture list, interpreter pin, code the pinned tests execute,
or per-line slope can justify the sanctioned re-seed: two reviewed
gate-definer changes. The first deletes the family under the
``[suite-cost-re-seed]`` marker; the second seeds from both runner
artifacts. The guard forbids raises and slope or identity edits on an
established family, so one change cannot delete and re-seed it.

The m1 source is the open seed PR's final-code-and-tests run, or the
newest in-window master run that actually measured that final tree.
It includes the seed change's own code and tests. The tree stays fixed
between that run and merge, apart from the thresholds write and
documentation-only paths. The recorded identity is never corrected by
hand to fit another tree (SV-CI-RATCHETS). Only the checkout identity
check imports the bench.

  python3 scripts/ci/suite_ratchet.py --seed m1.json --growth m2.json
  python3 scripts/ci/suite_ratchet.py --tighten m.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import reseed, thresholds
else:
    thresholds = importlib.import_module('thresholds')
    reseed = importlib.import_module('reseed')

# The checkout holding this script: the tree a seed lands on, and the
# root the bench's tree-line counter walks. Not the --thresholds path,
# which a test or a bot copies elsewhere.
REPO_ROOT = Path(__file__).resolve().parents[2]

CALIBRATION_GAP = Decimal('1.5')
TIGHTEN_HYSTERESIS = Decimal('1.5')
UNIT = thresholds.SUITE_COST_UNIT
PHASES = thresholds.SUITE_COST_PHASES
_IDENTITY = thresholds.SUITE_COST_IDENTITY
_family = thresholds.SUITE_COST_FAMILY
_COUNTS = 'million_instructions'


def _identity(path: Path):
    """The measurement's workload identity, or None when absent.

    The seed writes it beside the budgets, binding them to the workload
    they measured (SV-CI-RATCHETS, issue #524); the gate and the guard
    read it back against exactly the reading that produced it.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f'cannot read the bench measurement {path}: {error}') from None
    identity = data.get(_IDENTITY)
    if identity is None:
        return None
    if (isinstance(identity, bool) or not isinstance(identity, int)
            or identity <= 0):
        raise ValueError(
            f'{path}: {_IDENTITY} must be a positive integer')
    return identity


def _tree_identity() -> int:
    """The working tree's ``tests_tree_lines``, as the bench computes it.

    The anchor is this checkout (``REPO_ROOT``), the tree the seed lands
    on. Lazy ``suite_bench`` import: the data operations stay free of
    the bench's import surface -- pulling it in would import pytest --
    and only the CLI's seed mode needs the counter.
    """
    if __package__:
        # pylint: disable-next=relative-beyond-top-level,no-name-in-module,import-outside-toplevel
        from . import suite_bench
    else:
        suite_bench = importlib.import_module('suite_bench')
    return suite_bench._tests_tree_lines(  # pylint: disable=protected-access
        REPO_ROOT)


def _check_tree_identity(artifact: Path, identity) -> None:
    """Refuse a seed measured on a different tests tree than this one.

    The sanctioned re-seed measures the FINAL tree the seed lands on,
    the seed change's own edits included (SV-CI-RATCHETS): the working
    tree of the checkout running this command is that tree. A
    measurement whose recorded identity names another tree seeds
    budgets for a workload this tree does not present, and once a
    family exists the direction guard refuses the hand correction, so
    the wrong-tree seed has to fail here, before it is committed
    (issue #598). Missing identities are refused by seed(); this helper
    checks a supplied m1 identity against the checkout.
    """
    if identity is None:
        return
    tree_lines = _tree_identity()
    if identity == tree_lines:
        return
    raise ValueError(
        f'{artifact}: the measurement was taken on a tests tree of '
        f'{identity} lines, but the tree this seed lands on holds '
        f'{tree_lines}: the sanctioned re-seed measures the final tree '
        '(SV-CI-RATCHETS); seed from a measurement of THIS tree')


def _phase_counts(path: Path) -> dict:
    """The measurement file's per-phase counts, as one-place Decimals.

    A counts-less measurement (the process_time fallback) is refused:
    a budget operation has nothing to seed or tighten FROM, and an
    absent measurement must never read as a cheap one.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('instrument') != 'instruction_count':
            raise ValueError(f'{path}: instrument must be instruction_count')
        if data.get('hash_seed') != '0':
            raise ValueError(f'{path}: hash_seed must be 0 (PYTHONHASHSEED=0)')
        phases = data['phases']
        counts = {}
        for phase in PHASES:
            spelling = phases[phase][_COUNTS]
            if not isinstance(spelling, str):
                raise ValueError(
                    f'{path}: the {phase} phase carries no '
                    f'{_COUNTS} (the process_time fallback is not '
                    'gateable or seedable)')
            try:
                counts[phase] = thresholds.instruction_value(
                    Decimal(spelling), f'{path}: {phase}.{_COUNTS}')
            except InvalidOperation:
                raise ValueError(
                    f'{path}: {phase}.{_COUNTS} is not a number'
                ) from None
        return counts
    except (OSError, KeyError, TypeError,
            json.JSONDecodeError) as error:
        raise ValueError(
            f'cannot read the bench measurement {path}: {error}') from None


def seed(data, counts, identity=None, growth_counts=None, growth_identity=None):
    """Return a document with the family seeded, or raise when present.

    ``data`` is the RAW document as its bytes carry it: the seed's
    target is exactly the document the member is ABSENT from, which the
    loader accepts while the delete's marker sits inside the bounded
    walk's span — any commit from HEAD back to the last family-present
    commit (reseed.py) — so the seed commit need not carry the marker
    itself. Seeding over a recorded family is refused: overwriting
    would need its own justification, and the sanctioned one is the
    doctrine's re-seed -- delete the stale member first, commit the new
    counts second, both reviewed gate-definers -- not a quiet rewrite by
    a bot. Both measurements require identities; the larger growth tree
    supplies each phase's integer instruction cost per added line.
    """
    if _family in data:
        # PRESENCE, never emptiness: `"suite_cost": {}` is the family
        # PRESENT and malformed, and replacing it is the hand-raise's
        # doorway whatever its (broken) shape (issue #705; the reparse
        # writer carries the same test — issue #703).
        raise ValueError(
            f'{_family} is already recorded: seeding would overwrite '
            'recorded budgets. Delete the stale member under the '
            f'{reseed.MARKER} marker first if the workload '
            'legitimately changed.')
    if growth_counts is None:
        raise ValueError('seeding requires the growth measurement artifact')
    if identity is None or growth_identity is None:
        raise ValueError(f'seed and growth artifacts require {_IDENTITY}')
    identity = thresholds.suite_identity(identity, f'seed.{_IDENTITY}')
    growth_identity = thresholds.suite_identity(
        growth_identity, f'growth.{_IDENTITY}')
    if growth_identity <= identity:
        raise ValueError('growth tests_tree_lines must be strictly greater '
                         'than seed tests_tree_lines')
    candidate = dict(data)
    candidate[_family] = {_IDENTITY: identity}
    for phase in PHASES:
        measured = thresholds.instruction_value(
            counts[phase], f'{_family}.{phase}.measured')
        grown = thresholds.instruction_value(
            growth_counts[phase], f'growth.{phase}.{_COUNTS}')
        slope = ((grown - measured) * Decimal(1_000_000)
                 / (growth_identity - identity))
        candidate[_family][phase] = {
            'measured': measured,
            'floor': measured + CALIBRATION_GAP,
            'per_line': max(0, int(slope.to_integral_value(rounding=ROUND_CEILING))),
        }
    # Strict, by fact and not by default: the candidate this builds
    # always carries the family, so there is nothing for the marker to
    # excuse. The re-seed's tolerance is the loader's, not the seed's.
    return thresholds.normalise(candidate, False)


def load_for_seed(path):
    """The raw committed bytes a seed may target.

    A document carrying the family is loaded through the loader's
    strict validation; one the family predates is parsed leniently --
    trusted committed bytes -- and handed to seed() with the member
    absent, which is the one state seed() accepts.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    # PRESENCE, never emptiness, the loader's own rule (issue #705):
    # `"suite_cost": {}` is a family PRESENT and malformed — the loader
    # refuses it and the seed must not quietly replace it. A falsy test
    # would read the decoy as the absent family only the marked delete
    # may produce.
    if _family in data:
        return thresholds.load(path), False
    return data, True


def tightenable(data, counts, identity=None):
    """Every phase whose measurement justifies a tighten.

    Compare at the seed tree by subtracting the budgeted growth. Smaller
    trees never tighten: their absolute counts cannot describe the seed.
    Phases remain independent, with their slopes and shared anchor fixed.
    Compare exact non-negative normalized counts, then round qualifying
    targets upward to one decimal; negative counts skip with a seed signal.
    """
    if identity is None:
        raise ValueError(f'tighten measurement requires {_IDENTITY}')
    identity = thresholds.suite_identity(identity, f'measurement.{_IDENTITY}')
    candidate = thresholds.normalise(data, thresholds.verdict())
    seed_identity = candidate[_family].get(_IDENTITY, identity)
    if identity < seed_identity:
        return {}
    moves = {}
    for phase in PHASES:
        if phase not in counts:
            raise ValueError(f'the measurement carries no {phase} phase')
        recorded = candidate[_family][phase]['measured']
        measured = thresholds.instruction_value(
            counts[phase], f'{_family}.{phase}.measured')
        measured -= (Decimal(candidate[_family][phase].get('per_line', 0))
                     * max(0, identity - seed_identity) / Decimal(1_000_000))
        if measured < 0:
            print(f'{phase}: c_norm {measured}; budgeted growth exceeds '
                  'the reading — seed candidate; skipping tighten')
            continue
        if recorded - measured > TIGHTEN_HYSTERESIS:
            moves[phase] = measured.quantize(Decimal('0.1'), rounding=ROUND_CEILING)
    return moves


def update(data, counts, identity=None):
    """Return an updated document, or ``None`` when no tighten is due."""
    moves = tightenable(data, counts, identity)
    if not moves:
        return None
    candidate = thresholds.normalise(data, thresholds.verdict())
    for phase, measured in moves.items():
        candidate[_family][phase].update(
            measured=measured, floor=measured + CALIBRATION_GAP)
    return thresholds.normalise(candidate, thresholds.verdict())


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--seed', type=Path, metavar='MEASUREMENT',
                       help='write the committed seed for the family '
                            'from this measurement (refuses when the '
                            'family is already recorded)')
    modes.add_argument('--tighten', type=Path, metavar='MEASUREMENT',
                       help='lower the budgets of phases this '
                            'measurement beats by more than the '
                            'hysteresis; never raises')
    parser.add_argument('--growth', type=Path, metavar='MEASUREMENT',
                        help='required growth artifact accompanying --seed')
    parser.add_argument('--thresholds', type=Path,
                        default=thresholds.THRESHOLDS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.seed is not None and args.growth is None:
            raise ValueError('--seed requires the --growth measurement artifact')
        if args.tighten is not None and args.growth is not None:
            raise ValueError('--growth accompanies --seed, never --tighten')
        artifact = args.seed if args.seed is not None else args.tighten
        assert artifact is not None  # The required mode group guarantees it.
        counts = _phase_counts(artifact)
        if args.seed is not None:
            assert args.growth is not None  # Checked before reading artifacts.
            data, _seeded_fresh = load_for_seed(args.thresholds)
            identity = _identity(args.seed)
            _check_tree_identity(args.seed, identity)
            candidate = seed(data, counts, identity,
                             _phase_counts(args.growth), _identity(args.growth))
            thresholds.write(args.thresholds, candidate)
            for phase in PHASES:
                print(f'seeded {phase} '
                      f'{candidate[_family][phase]["measured"]} {UNIT} '
                      f'(ceiling {candidate[_family][phase]["floor"]}, '
                      f'per_line {candidate[_family][phase]["per_line"]})')
            return 0
        data = thresholds.load(args.thresholds)
        if not data.get(_family):
            # A tree inside the re-seed window — the delete and every
            # commit that lands on it before the seed — carries no
            # budget to tighten. Tightening nothing is the whole
            # answer, and saying so beats a crash on an absent family
            # the walk found the marker for.
            print('no suite_cost budget — re-seed in flight: '
                  'nothing to tighten')
            return 0
        candidate = update(data, counts, _identity(artifact))
        if candidate is None:
            print('no eligible suite phase beat its recorded budget by more than '
                  f'{TIGHTEN_HYSTERESIS}: nothing to tighten')
            return 0
        for phase in PHASES:
            before = data[_family][phase]
            after = candidate[_family][phase]
            if after != before:
                print(f'tightened {phase} '
                      f'{before["floor"]} -> {after["floor"]} {UNIT} '
                      f'(measured {after["measured"]})')
        thresholds.write(args.thresholds, candidate)
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
