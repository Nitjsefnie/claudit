#!/usr/bin/env python3
"""Did the fixture corpus walk every parse path the pass can reach?

``reparse_ast`` names what the pass CAN reach; this is the half that
checks it DID, and the gate the reparse bench runs beside its own
measurement (issue #503). A corpus that stopped exercising a parse path is
a worse measurement than no measurement, because the number it produced no
longer means what the bench says it means.

TWO CORPORA, TWO CLAIMS, and the gate makes both.

- The UNION — the bench's own mirror plus every committed sample — answers
  "do the committed fixtures walk every reachable parse path?". It is the
  population: the mirror is nine transcripts, one of which is a single
  ``ls``, so it cannot exercise the Bash-churn machinery alone, and
  inventing a second copy of every churn shape to fix that would be worse
  than reusing the samples the parser tests already drive.

- The MIRROR ALONE answers "does the corpus the bench's NUMBER is measured
  over still carry every format?". This is checked here, not left to a
  test elsewhere: the samples duplicate all four formats, so a mirror that
  loses one leaves the union green while the number stops meaning what the
  bench says it means. It is the exact regression issue #503 exists to
  close, and the gate has to be the thing that catches it.

- DENY BY DEFAULT. ``reparse_surface_allowlist.json`` is the only way out
  of a finding, it is a reviewed data file, and every entry carries a
  reason. An entry naming a function the walk no longer reaches is itself
  an error (SV-CI-RATCHETS' direction guard, applied here): a deleted
  function must delete its excuse.

- FAIL CLOSED. An absent counter, an unreadable module, a missing
  allowlist or an excuse for a function that is gone all report a reason
  and refuse; none of them reports "nothing unexercised". An absent
  measurement must never read as a passing one — the same rule the bench's
  NOT MEASURED line follows.

  python3 scripts/ci/reparse_surface.py            # the gate, as a process
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
# A sibling in a directory that is not a package, imported only after that
# directory is on sys.path — which is why it is not at the top.
# pylint: disable-next=wrong-import-position
from reparse_ast import code_objects, reachable_surface  # noqa: E402

ALLOWLIST = Path(__file__).resolve().parent / 'reparse_surface_allowlist.json'


def allowlist_entries(path: Path = ALLOWLIST) -> dict:
    """The reviewed excuses, each a reason string."""
    if not path.exists():
        raise ValueError(f'the surface allowlist is missing: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('the surface allowlist is not an object')
    for key, reason in data.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f'the allowlist entry {key} carries no reason: an excuse '
                'without one is the hole this file exists to close')
    return data


def unexercised(surface: dict, executed, allowed: dict) -> list:
    """Reachable functions the pass never ran, minus the allowlist."""
    return sorted(name for name in surface
                  if name not in executed and name not in allowed)


def stale_allowlist(surface: dict, allowed: dict) -> list:
    """Allowlist entries naming a function the walk no longer reaches.

    A deleted or renamed function must delete its excuse in the same
    change: an entry nothing reads is a rule that outlived its subject.
    """
    return sorted(name for name in allowed if name not in surface)


# --- the corpus the gate measures --------------------------------------------

# The committed fixture SAMPLES, beside the mirror: one hand-crafted record
# per parse behaviour, driven by tests/test_parse.py. The gate walks them
# too, because they are the corpus that already exercises the Bash-churn
# and edit shapes the mirror never carries — reusing them is cheaper than
# inventing a second copy of each, and they are already reviewed.
SURFACE_SAMPLES = ('parser', 'codex')


def corpus(base_entries, entry_type, root: Path = ROOT) -> list:
    """Every transcript the gate measures: `base_entries`, plus every
    committed sample.

    A sample is entered under its own `fixtures/`-relative key, which no
    layout rule claims and no ingest ever fetches: the gate parses bytes,
    it does not walk a bucket. The mirror's own keys are bucket-qualified
    and stay exactly as the listing walk made them.

    `entry_type` is the bench's own `Entry`, named rather than imported:
    this module is the gate, the bench is the measurement, and a gate that
    imported the bench's corpus would make the measurement decide what the
    gate looks at.
    """
    entries = list(base_entries)
    seen = {entry.key for entry in entries}
    fixtures = root / 'fixtures'
    for name in SURFACE_SAMPLES:
        for path in sorted((fixtures / name).rglob('*.jsonl')):
            key = path.relative_to(fixtures).as_posix()
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry_type(key, None, path.read_bytes(), None))
    return entries


def gap(entries, run_pass, root: Path = ROOT) -> tuple:
    """What the corpus does NOT exercise of the surface the pass can reach.

    Returns ``(missing, note)``. ``note`` is non-empty when the answer is
    NOT "nothing missing": an unreadable module, an absent counter, a
    missing allowlist, an excuse for a function that is gone, a reachable
    function the code-object table could not resolve. The gate fails closed
    on a note, because an unmeasurable surface and a fully covered one must
    not read alike.
    """
    try:
        surface = reachable_surface(root)
        allowed = allowlist_entries()
    except (OSError, ValueError) as error:
        return None, str(error)
    stale = stale_allowlist(surface, allowed)
    if stale:
        return None, ('the surface allowlist excuses functions the walk no '
                      'longer reaches: ' + ', '.join(stale))
    table = code_objects(surface)
    if len(table) != len(surface):
        # A name the table cannot resolve to a code object can never be
        # reported as executed, so it would read as covered for ever.
        return None, (f'{len(surface) - len(table)} reachable function(s) '
                      'have no code object the counter can name')
    counts = _phases().measure_counts(
        entries, run_pass, passes=1, warmup=0, qualnames=table)
    if not counts.available:
        return None, counts.reason
    return unexercised(surface, counts.executed, allowed), ''


def _phases():
    """The counting instrument, imported by path like its siblings.

    ``scripts/ci`` is not a package, and importing it as one would make
    every sibling's `importlib.import_module('...')` resolve twice under
    two module names.
    """
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module('reparse_phases')


#: Every format `parse_lanes.sniff_format` recognizes. A mirror that
#: stops carrying one of them has stopped exercising that lane's parse
#: path, and the union below would still be green — the samples duplicate
#: all four. So the gate makes the mirror's coverage a claim of its own
#: rather than leaving it to one assertion elsewhere.
SNIFFED_FORMATS = ('claude', 'codex', 'kimi-code', 'legacy')


def mirror_gap(mirror_entries) -> list:
    """Formats the BENCH's own corpus no longer carries.

    The union answers "do the committed fixtures walk every parse path?".
    This answers the narrower question the bench's number depends on: does
    the corpus that number is measured over still carry every format?
    """
    # Imported here so this module stays importable without the backend
    # package resolved at import time (the gate is a subprocess).
    # pylint: disable-next=import-outside-toplevel
    from backend.parse_lanes import sniff_format
    return sorted(set(SNIFFED_FORMATS)
                  - {sniff_format(entry.blob) for entry in mirror_entries})


def report(missing, note: str = '', absent: list | None = None) -> str:
    """The human line(s) for a gate run: what ran, what did not."""
    if note:
        return f'parse surface NOT MEASURED: {note}'
    absent = absent or []
    if absent and not missing and not note:
        return ('parse surface: the bench corpus no longer carries: '
                + ', '.join(absent))
    if not missing:
        return 'parse surface: every reachable function was exercised'
    body = '\n'.join(f'  never exercised: {name}' for name in missing)
    return (f'parse surface: {len(missing)} reachable function(s) the '
            f'fixture corpus never exercises.\n{body}\n'
            'Either add a fixture that walks the path, or record the reason '
            f'in {ALLOWLIST.name}.')


def main(argv=None) -> int:
    """The gate as a process: 0 when every reachable function ran, 1
    naming the ones that did not.

    The corpus and the pass come from the bench, imported lazily inside
    this call — the bench imports this module at load, and an import
    cycle between them would make which of the two is "the" gate depend on
    which one a caller happened to load first.
    """
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    bench = importlib.import_module('reparse_bench')
    mirror = bench.corpus()
    missing, note = gap(corpus(mirror, bench.Entry), bench.run_pass)
    absent = mirror_gap(mirror) if not (note or missing) else []
    print(report(missing, note, absent), file=sys.stderr)
    return 1 if (note or missing or absent) else 0


if __name__ == '__main__':
    raise SystemExit(main())
