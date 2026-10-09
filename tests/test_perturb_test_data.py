"""SV-TEST-DATA's second half: scripts/ci/perturb_test_data.py under test.

The CI leg this script serves runs the suite against a perturbed tree
that simulates what the automated refresh does — every rate row gains
five appended entries per run (×2.0, ×0.37, and a per-row irregular
factor in [0.61, 1.47) scaling the whole row; then five independent
per-field factors; then a single-field move) and the three version
constants move up one — so a test that pins repository-managed data
fails as a test failure. These tests drive the perturbation over a
MINIMAL synthetic pricing.json in tmp_path and a synthetic constants
bumper; one pin test perturbs a tmp COPY of the committed document,
which it only ever reads — the tree's own file is never mutated.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.refresh_fixture_builders import (
    DEFAULT_ROW, RATES_A, RATES_B, seed_doc as build_seed_doc,
)


from backend import pricing
from backend.pricing_document import serialize_pricing_doc

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "perturb_test_data.py"

RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
_CACHE_WRITE_FIELDS = {"create_5m", "create_1h"}
NOTE_PREFIX = "sv-test-data perturbation: rates ×"
RATES_ZERO = {"fresh": 0, "create_5m": 0, "create_1h": 0, "read": 0,
              "output": 0}
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


def _effective_rates(entry: dict) -> dict:
    """Expand omitted cache-write rates to the entry's fresh rate."""
    return {field: entry.get(field, entry["fresh"])
            if field in _CACHE_WRITE_FIELDS else entry[field]
            for field in RATE_FIELDS}


def _load():
    spec = importlib.util.spec_from_file_location("perturb_test_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["perturb_test_data"] = module
    spec.loader.exec_module(module)
    return module


perturb_module = _load()


def _seed_doc() -> dict:
    """One dated model row, one all-zero row, one scheduled provider row."""
    doc = build_seed_doc(
        models={
            "acme/acme-9": [
                {"from": None, **RATES_A},
                {"from": "2026-06-01T00:00:00Z", **RATES_B},
            ],
            "free/acme-0": [{"from": None, **RATES_ZERO}],
        },
        providers={
            "acme/acme-9": {
                "HostCo": [{"from": "2026-01-01T00:00:00Z", **RATES_A,
                            "schedule": [{"days": ["saturday", "sunday"],
                                          "start": 2200, "end": 200,
                                          "rates": RATES_B}]}],
            },
        },
        fetched="2026-06-01T00:00:00Z",
    )
    doc["models"] = dict(sorted(doc["models"].items()))
    return doc


def _seed_tree(tmp_path: Path) -> tuple[Path, Path, dict]:
    doc = _seed_doc()
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    constants_path.write_text(
        '"""Synthetic constants."""\n'
        'PARSER_VERSION = "81"\n'
        'PRICING_VERSION = "5"\n'
        'MARKER_READER_VERSION = "1"\n'
        'OTHER = "untouched"\n',
        encoding="utf-8")
    return pricing_path, constants_path, doc


def _run(tmp_path: Path) -> tuple[dict, dict, str, Path]:
    """Seed, run the perturbation once, read the tree back."""
    pricing_path, constants_path, doc = _seed_tree(tmp_path)
    perturb_module.perturb_pricing(pricing_path, seed=int(NOW.timestamp()))
    perturb_module.bump_constants(constants_path)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    text = pricing_path.read_text(encoding="utf-8")
    return doc, perturbed, text, constants_path


def _run_twice(tmp_path: Path) -> tuple[dict, dict, Path]:
    """Seed, run the perturbation twice, read the tree back."""
    pricing_path, constants_path, doc = _seed_tree(tmp_path)
    for _ in range(2):
        perturb_module.perturb_pricing(pricing_path, seed=int(NOW.timestamp()))
        perturb_module.bump_constants(constants_path)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    return doc, perturbed, constants_path


def _histories(doc: dict) -> list[list[dict]]:
    """Every row's entry list: each model's, then each provider host's."""
    return ([doc["models"][key] for key in doc["models"]]
            + [doc["providers"][model][host]
               for model, hosts in doc["providers"].items()
               for host in hosts])


def _note_factor(entry: dict) -> str:
    """The factor text a perturbation note names, e.g. "2.0" or "1.234567"."""
    assert entry["note"].startswith(NOTE_PREFIX), entry["note"]
    return entry["note"][len(NOTE_PREFIX):].split(" ")[0]


def _moved_field_of(entry: dict) -> str:
    """The field name a single-field note names, e.g. "read"."""
    prefix = "sv-test-data perturbation: single-field move ("
    assert entry["note"].startswith(prefix), entry["note"]
    assert entry["note"].endswith(")"), entry["note"]
    return entry["note"][len(prefix):-1]


def test_every_row_gains_exactly_five_entries(tmp_path):
    doc, perturbed, _text, _path = _run(tmp_path)
    compact = json.loads(serialize_pricing_doc(doc))
    for key, entries in compact["models"].items():
        assert len(perturbed["models"][key]) == len(entries) + 5
        assert perturbed["models"][key][:-5] == entries
    for model, hosts in compact["providers"].items():
        for host, entries in hosts.items():
            got = perturbed["providers"][model][host]
            assert len(got) == len(entries) + 5
            assert got[:-5] == entries


def test_every_new_entry_comes_after_its_predecessor(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    for entries in _histories(perturbed):
        for before, after in zip(entries, entries[1:]):
            previous = before.get("from")
            stamp = after.get("from")
            assert isinstance(stamp, str)
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            assert parsed.tzinfo is not None
            if previous is not None:
                earlier = datetime.fromisoformat(
                    previous.replace("Z", "+00:00"))
                assert parsed > earlier


def test_the_first_three_new_notes_name_the_whole_row_factors(tmp_path):
    """The three whole-row classes keep their notes: the appended set is
    now five entries, so these are the FIRST three appended notes; the
    two new classes' notes are the dedicated tests' business."""
    doc, perturbed, _text, _path = _run(tmp_path)
    for entries, original in zip(_histories(perturbed), _histories(doc)):
        appended = entries[len(original):]
        assert len(appended) == 5
        factors = [_note_factor(entry) for entry in appended[:3]]
        assert factors[:2] == ["2.0", "0.37"]
        assert float(factors[2]) not in (2.0, 0.37)
        assert 0.61 <= float(factors[2]) < 1.47


def test_the_irregular_factor_is_bounded_with_six_decimals(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    texts = [_note_factor(entries[-3]) for entries in _histories(perturbed)]
    for text in texts:
        assert 0.61 <= float(text) < 1.47
        _whole, _dot, decimals = text.partition(".")
        assert len(decimals) >= 6
        assert float(text) != 1.0


def test_two_rows_get_different_irregular_factors(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    factors = [float(_note_factor(entries[-3]))
               for entries in _histories(perturbed)]
    assert len(set(factors)) == len(factors)


def test_the_new_rates_differ_and_scale_the_previous_newest(tmp_path):
    """The three WHOLE-ROW entries scale the row's previous newest
    entry, and every appended entry's every field differs from that
    newest (the zero→1.0 convention covers zero, and the single-field
    entry copies the per-field entry — which moved every field)."""
    doc, perturbed, _text, _path = _run(tmp_path)
    for entries, original in zip(_histories(perturbed), _histories(doc)):
        base = original[-1]
        appended = entries[len(original):]
        for entry in appended:
            rates = _effective_rates(entry)
            for field in RATE_FIELDS:
                assert rates[field] != base[field]
                assert math.isfinite(rates[field]) and rates[field] >= 0
        for entry in appended[:3]:
            factor = float(_note_factor(entry))
            rates = _effective_rates(entry)
            for field in RATE_FIELDS:
                expected = base[field] * factor if base[field] else 1.0
                assert rates[field] == expected


def test_the_note_names_the_factor_that_was_applied(tmp_path):
    """Each WHOLE-ROW entry's note spells out its factor exactly, and
    the rates are the row's previous newest under that factor."""
    doc, perturbed, _text, _path = _run(tmp_path)
    checked = 0
    for entries, original in zip(_histories(perturbed), _histories(doc)):
        base = original[-1]
        for entry in entries[len(original):][:3]:
            text = _note_factor(entry)
            assert entry["note"] == (
                f"{NOTE_PREFIX}{text} (a zero rate becomes one)")
            checked += 1
            factor = float(text)
            rates = _effective_rates(entry)
            for field in RATE_FIELDS:
                expected = base[field] * factor if base[field] else 1.0
                assert rates[field] == expected
    assert checked == len(_histories(perturbed)) * 3


def test_an_offset_spelled_newest_stamp_perturbs_cleanly(tmp_path):
    """(issue #264) A document whose newest real stamp is spelled with a
    ±HH:MM offset perturbs cleanly: `_stamp_after` normalises to UTC
    before formatting, so the appended stamps carry the INSTANT — not
    the offset-local wall time mislabeled Z, five hours early — the
    loader accepts the document, and every appended stamp is strictly
    after its predecessor's instant."""
    doc = _seed_doc()
    # acme/acme-9's newest entry, spelled five hours behind UTC: the
    # instant is 17:00:00Z.
    doc["models"]["acme/acme-9"][-1]["from"] = "2026-10-01T12:00:00-05:00"
    pricing_path, _constants_path, _original = _seed_tree(tmp_path)
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows, _seed, _base = perturb_module.perturb_pricing(
        pricing_path, seed=int(NOW.timestamp()))
    assert rows == 4, "the seed doc's four rate rows"
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert pricing.load_tables(perturbed)
    for entries in _histories(perturbed):
        instants = [None if entry.get("from") is None else
                    datetime.fromisoformat(
                        entry["from"].replace("Z", "+00:00"))
                    for entry in entries]
        # Only a row's FIRST entry may carry from: null; every later
        # entry's instant must beat its (non-null) predecessor's.
        for earlier, later in zip(instants, instants[1:]):
            assert later is not None
            if earlier is not None:
                assert later > earlier


def test_every_row_gains_a_per_field_entry(tmp_path):
    """The fourth appended entry scales each field by its OWN seeded
    factor — from (seed, row key, field), in [0.61, 1.47), never 1.0 —
    so a within-row ratio the data once had does not survive. The five
    factors of one row are DISTINCT: dropping `field` from the digest
    preserves every ratio the entry exists to break."""
    doc, perturbed, _text, _path = _run(tmp_path)
    for (key, entries), (_key, original) in zip(
            _rows_of(perturbed), _rows_of(doc), strict=True):
        base = original[-1]
        assert len(entries) == len(original) + 5
        entry = entries[len(original) + 3]
        assert entry["note"] == "sv-test-data perturbation: independent per-field factors"
        factors = []
        rates = _effective_rates(entry)
        for field in RATE_FIELDS:
            text = perturb_module._field_factor(  # pylint: disable=protected-access
                int(NOW.timestamp()), key, field)
            assert 0.61 <= float(text) < 1.47 and float(text) != 1.0 \
                and len(text.partition(".")[2]) == 6
            factors.append(float(text))
            assert rates[field] == (
                base[field] * float(text) if base[field] else 1.0)
        assert len(set(factors)) == len(RATE_FIELDS)


def test_every_row_gains_a_single_field_entry(tmp_path):
    """The fifth appended entry moves EXACTLY one seeded field — scaled
    by its own seeded factor, zero→1.0 — and copies the per-field
    entry's other four: a ratio pin (read == fresh * k) flips the
    moment the seeded choice lands on one of its fields, whatever the
    row prices."""
    doc, perturbed, _text, _path = _run(tmp_path)
    for (key, entries), (_key, original) in zip(
            _rows_of(perturbed), _rows_of(doc), strict=True):
        base = original[-1]
        assert len(entries) == len(original) + 5
        entry, per_field = (entries[len(original) + 4],
                            entries[len(original) + 3])
        moved = _moved_field_of(entry)
        assert moved in RATE_FIELDS
        factor = float(perturb_module._single_field_factor(  # pylint: disable=protected-access
            int(NOW.timestamp()), key, moved))
        assert 0.61 <= factor < 1.47 and factor != 1.0
        assert _effective_rates(entry)[moved] == (
            base[moved] * factor if base[moved] else 1.0)
        for other in RATE_FIELDS:
            if other != moved:
                assert (_effective_rates(entry)[other]
                        == _effective_rates(per_field)[other])


def test_the_single_field_choice_follows_the_seed(tmp_path):
    """The moved field is seeded in (seed, row key): another seed moves
    a different field on at least one row."""
    choices = []
    pricing_path, _constants_path, _doc = _seed_tree(tmp_path)
    pristine = pricing_path.read_text(encoding="utf-8")
    for offset in (0, 1):
        pricing_path.write_text(pristine, encoding="utf-8")
        perturb_module.perturb_pricing(
            pricing_path, seed=int(NOW.timestamp()) + offset)
        perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
        choices.append([_moved_field_of(entries[-1])
                        for entries in _histories(perturbed)])
    assert choices[0] != choices[1]


def test_the_same_seed_is_byte_identical_on_the_same_document(tmp_path):
    """Same document, same seed → byte-identical output: the shuffle,
    every factor, every appended stamp and the new entry classes derive
    from (document, seed) alone."""
    pricing_path, _constants_path, _doc = _seed_tree(tmp_path)
    pristine = pricing_path.read_text(encoding="utf-8")
    texts = []
    for _ in range(2):
        pricing_path.write_text(pristine, encoding="utf-8")
        perturb_module.perturb_pricing(pricing_path,
                                       seed=int(NOW.timestamp()))
        texts.append(pricing_path.read_text(encoding="utf-8"))
    assert texts[0] == texts[1]


def test_zeros_become_one_under_every_factor(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    for entry in perturbed["models"]["free/acme-0"][-5:]:
        assert all(_effective_rates(entry)[field] == 1
                   for field in RATE_FIELDS)


def test_appended_stamps_step_one_second_apart_per_row(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    for entries in _histories(perturbed):
        stamps = [datetime.fromisoformat(entry["from"].replace("Z", "+00:00"))
                  for entry in entries[-5:]]
        for earlier, later in zip(stamps, stamps[1:]):
            assert later - earlier == timedelta(seconds=1)


def test_the_row_shuffle_is_observable_in_the_stamp_order(tmp_path):
    """The rows are processed in a seeded-shuffled order: the order the
    rows receive their stamps differs from the file's row order."""
    pricing_path, _constants_path, _doc = _seed_tree(tmp_path)
    seed_doc = json.loads(pricing_path.read_text(encoding="utf-8"))
    seed_doc["models"] = {f"acme/model-{index:02d}":
                          [{"from": None, **RATES_A}]
                          for index in range(12)}
    seed_doc["models"]["claude-opus-4-7"] = [
        dict(DEFAULT_ROW)]
    pricing_path.write_text(
        json.dumps(seed_doc, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    perturb_module.perturb_pricing(pricing_path, seed=int(NOW.timestamp()))
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    keys = sorted(seed_doc["models"])
    by_stamp = sorted(
        keys,
        key=lambda key: datetime.fromisoformat(
            perturbed["models"][key][1]["from"].replace("Z", "+00:00")))
    assert by_stamp != keys
    assert sorted(by_stamp) == keys


def test_the_same_seed_is_byte_identical_on_a_fresh_run(tmp_path):
    """Two fresh trees perturbed with the same seed come out identical —
    the shuffle, every factor and every appended stamp derive from
    (document, seed) alone."""
    texts = []
    for name in ("one", "two"):
        tree = tmp_path / name
        tree.mkdir()
        pricing_path, _constants_path, _doc = _seed_tree(tree)
        perturb_module.perturb_pricing(pricing_path, seed=int(NOW.timestamp()))
        texts.append(pricing_path.read_text(encoding="utf-8"))
    assert texts[0] == texts[1]


def test_the_schedule_is_untouched(tmp_path):
    doc, perturbed, _text, _path = _run(tmp_path)
    original = doc["providers"]["acme/acme-9"]["HostCo"][0]["schedule"]
    kept = perturbed["providers"]["acme/acme-9"]["HostCo"][0]["schedule"]
    assert kept == original
    appended = perturbed["providers"]["acme/acme-9"]["HostCo"][-1]
    assert "schedule" not in appended


def test_the_fetch_stamp_and_openrouter_section_are_untouched(tmp_path):
    doc, perturbed, _text, _path = _run(tmp_path)
    assert (perturbed["provider_rates_fetched"]
            == doc["provider_rates_fetched"])
    assert perturbed["openrouter"] == doc["openrouter"]


def test_the_perturbed_doc_passes_the_loaders_validation(tmp_path):
    """The loaded tables carry the perturbation's COMPUTED values, not
    an echo of the file's own last entry: for a known seed, the newest
    rates and the two windows the last stamps close are derived here
    from the seeded factors, then asserted against `load_tables`."""
    seed = 42
    pricing_path, _constants_path, doc = _seed_tree(tmp_path)
    perturb_module.perturb_pricing(pricing_path, seed=seed)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    tables = pricing.load_tables(perturbed)
    for key in doc["models"]:
        base = doc["models"][key][-1]
        moved = perturb_module._moved_field(seed, key)  # pylint: disable=protected-access
        per_field = {
            field: (base[field] * float(
                perturb_module._field_factor(  # pylint: disable=protected-access
                    seed, key, field))
                    if base[field] else 1.0)
            for field in RATE_FIELDS}
        factor = float(
            perturb_module._single_field_factor(  # pylint: disable=protected-access
                seed, key, moved))
        expected = {**per_field,
                    moved: base[moved] * factor if base[moved] else 1.0}
        assert tables["MODEL_RATES"][key] == expected, key
        irregular = float(perturb_module._irregular_factor(seed, key))  # pylint: disable=protected-access
        # The window the FINAL stamp closes holds the per-field entry;
        # the one behind it, the seeded irregular whole-row entry.
        windows = tables["DATED_RATES"][key]
        assert windows[-1] == (datetime.fromisoformat(
            perturbed["models"][key][-1]["from"].replace("Z", "+00:00")),
            per_field)
        assert windows[-2] == (datetime.fromisoformat(
            perturbed["models"][key][-2]["from"].replace("Z", "+00:00")),
            {field: base[field] * irregular if base[field] else 1.0
             for field in RATE_FIELDS})
    assert len(tables["RATE_EPOCHS"]) > 0


def test_the_file_stays_in_the_canonical_layout(tmp_path):
    _doc, perturbed, text, _path = _run(tmp_path)
    assert text == json.dumps(perturbed, indent=2, sort_keys=True) + "\n"


def test_the_constants_file_bumps_exactly_plus_one(tmp_path):
    _doc, _perturbed, _text, constants_path = _run(tmp_path)
    lines = constants_path.read_text(encoding="utf-8").splitlines()
    assert 'PARSER_VERSION = "82"' in lines
    assert 'PRICING_VERSION = "6"' in lines
    assert 'MARKER_READER_VERSION = "2"' in lines
    assert 'OTHER = "untouched"' in lines
    assert len(lines) == 5


def test_a_future_newest_stamp_still_bounds_its_row(tmp_path):
    """A row whose newest real stamp is newer than everything else —
    including the seed instant — still bounds its own appended stamps:
    the counter is based at the document's newest real stamp, which is
    that row's own."""
    pricing_path, _constants_path, doc = _seed_tree(tmp_path)
    future = NOW + timedelta(days=30)
    doc["models"]["acme/acme-9"][-1]["from"] = future.strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    perturb_module.perturb_pricing(pricing_path, seed=int(NOW.timestamp()))
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    stamps = [datetime.fromisoformat(entry["from"].replace("Z", "+00:00"))
              for entry in perturbed["models"]["acme/acme-9"][-5:]]
    for stamp in stamps:
        assert stamp > future
    assert stamps == sorted(stamps)


def _real_pricing_text() -> str:
    """The committed document's text, to perturb on a tmp copy.

    A function-local bind: the SV-TEST-DATA guard's path-bind shape
    flags only module-level binds of the committed path."""
    return (ROOT / "src" / "pricing.json").read_text(encoding="utf-8")


def _rows_of(doc: dict) -> list[tuple[str, list]]:
    """Every rate row as (row key, entry list): models first, then hosts."""
    rows = list(doc["models"].items())
    for model, hosts in doc["providers"].items():
        rows.extend((f"{model} via {host}", entries)
                    for host, entries in hosts.items())
    return rows


def _newest_real_stamp(entries: list) -> datetime | None:
    """The newest non-null `from` in one row, or None when it has none."""
    stamps = [datetime.fromisoformat(entry["from"].replace("Z", "+00:00"))
              for entry in entries if entry.get("from") is not None]
    return max(stamps) if stamps else None


def _document_max_stamp(rows: list[tuple[str, list]]) -> datetime:
    """The newest real stamp anywhere in the document (some row is dated)."""
    return max(stamp for stamp in
               (_newest_real_stamp(entries) for _key, entries in rows)
               if stamp is not None)


_TABLE_NAMES = ("MODEL_RATES", "DATED_RATES", "PROVIDER_RATES",
                "PROVIDER_DATED_RATES", "PROVIDER_STARTS",
                "PROVIDER_SCHEDULES", "RATE_EPOCHS")


def _rate_under(doc: dict, model: str, ts: datetime,
                provider: str) -> dict:
    """`rate_for` as the perturbed document answers it: its tables loaded
    and swapped in for the module's, the pristine globals restored after
    so no other test ever sees them."""
    tables = pricing.load_tables(doc)
    saved = {name: getattr(pricing, name) for name in _TABLE_NAMES}
    for name in _TABLE_NAMES:
        setattr(pricing, name, tables[name])
    try:
        return pricing.rate_for(model, ts, provider)
    finally:
        for name, value in saved.items():
            setattr(pricing, name, value)


def _assert_pins_read_pristine(perturbed: dict) -> None:
    """Each pinned (model, provider) pair resolves through the perturbed
    document's tables to exactly what the pristine tables answer at
    SEEDED, and still answers through the provider table: a host a
    refresh withdrew must RED this mirror so it gets updated alongside
    the pins it mirrors (SV-TEST-DATA)."""
    for model, provider in PINNED_ROWS:
        resolution = pricing.resolve(model, SEEDED, provider)
        assert (resolution.key, provider) in pricing.PROVIDER_RATES, \
            (model, provider)
        pristine_rates = pricing.rate_for(model, SEEDED, provider)
        assert _rate_under(perturbed, model, SEEDED, provider) == \
            pristine_rates, (model, provider)


# The (model, provider) pairs of the six SEEDED closed-window pins in
# tests/test_provider_pricing.py (SEEDED below is that module's own
# instant, when the provider table was seeded), mirrored because those
# pins are inline assertions, not importable data. No rate is pinned as
# a literal here: both sides of every comparison are derived at run
# time, the pristine side from the tree's own tables (SV-TEST-DATA).
SEEDED = datetime(2026, 9, 24, 22, 3, 13, tzinfo=timezone.utc)
PINNED_ROWS = (
    # test_a_record_with_a_provider_is_priced_from_the_provider_table,
    # test_two_providers_of_one_model_price_differently
    ("deepseek/deepseek-v4.1-flash", "Novita"),
    ("deepseek/deepseek-v4.1-flash", "Morph"),
    # test_baseten_bills_the_global_endpoint_the_keys_can_reach
    ("deepseek/deepseek-v4.1-flash", "BaseTen"),
    # test_modal_glm_carries_its_one_remaining_endpoint
    ("z-ai/glm-5.3-flash", "Modal"),
    # test_the_dated_permaslug_resolves_to_the_same_row_as_the_slug
    ("deepseek/deepseek-v4-flash-0731", "Cohere"),
    ("deepseek/deepseek-v4-flash-20260731", "Cohere"),
    # test_the_permaslug_never_takes_the_undated_models_rate
    ("deepseek/deepseek-v4-flash", "Novita"),
    ("deepseek/deepseek-v4-flash-20260731", "Novita"),
)


def test_appended_stamps_decouple_from_the_seed_and_pins_hold(tmp_path):
    """ANY seed is valid (issue #227): the run seed drives the factors and
    the shuffle, never a `from` stamp. The appended stamps derive from
    the document's own newest real stamp, so even a seed far older than
    the data — 42, the historical case that clamped the appended entries
    into closed windows — leaves every appended entry after its row's
    newest real stamp, and the six closed-window pins of
    tests/test_provider_pricing.py keep reading their pristine rates at
    SEEDED."""
    pristine = json.loads(_real_pricing_text())
    document_max = _document_max_stamp(_rows_of(pristine))
    for seed in (42, int(time.time())):
        tree = tmp_path / str(seed)
        tree.mkdir()
        pricing_path = tree / "pricing.json"
        pricing_path.write_text(_real_pricing_text(), encoding="utf-8")
        perturb_module.perturb_pricing(pricing_path, seed=seed)
        perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))

        for (key, real_entries), (_key, entries) in zip(
                _rows_of(pristine), _rows_of(perturbed), strict=True):
            assert len(entries) == len(real_entries) + 5
            floor = (_newest_real_stamp(real_entries) or document_max)
            for entry in entries[-5:]:
                appended = datetime.fromisoformat(
                    entry["from"].replace("Z", "+00:00"))
                assert appended > floor, (seed, key, entry["from"], floor)

        _assert_pins_read_pristine(perturbed)


def test_a_second_run_appends_further_entries_without_corrupting(tmp_path):
    doc, perturbed, constants_path = _run_twice(tmp_path)
    for key, entries in doc["models"].items():
        assert len(perturbed["models"][key]) == len(entries) + 10
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            assert len(perturbed["providers"][model][host]) == len(entries) + 10
    assert pricing.load_tables(perturbed)
    lines = constants_path.read_text(encoding="utf-8").splitlines()
    assert 'PARSER_VERSION = "83"' in lines
    assert 'PRICING_VERSION = "7"' in lines
    assert 'MARKER_READER_VERSION = "3"' in lines


def test_a_tree_with_no_rate_rows_is_refused(tmp_path):
    """A zero-row perturbation would pass the leg constants-only — a
    partial-vacuous pass that proves nothing about the rate half."""
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(json.dumps({
        "models": {}, "providers": {},
        "provider_rates_fetched": "2026-06-01T00:00:00Z",
    }), encoding="utf-8")
    constants_path.write_text('PARSER_VERSION = "81"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no rate rows"):
        perturb_module.perturb_pricing(pricing_path, seed=int(NOW.timestamp()))


def test_the_script_answers_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        perturb_module.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "perturb" in out
    assert "×2.0" in out and "×0.37" in out
    assert "per-field" in out and "single-field" in out
    assert "--seed" in out


def test_the_run_report_names_the_entries_and_their_factors(tmp_path, capsys):
    """main()'s printed line states what the run applied — five appended
    entries per row, naming the classes (×2.0, ×0.37, the seeded per-row
    irregular factor, the independent per-field factors, and the
    single-field moves), the seed, and the stamp base — so a CI-leg log
    shows the perturbation, and names the seed a rerun can reproduce,
    without reading the tree."""
    pricing_path, constants_path, _doc = _seed_tree(tmp_path)
    exit_code = perturb_module.main(["--pricing", str(pricing_path),
                                     "--constants", str(constants_path),
                                     "--seed", "42"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "5 entries per row" in out
    assert "×2.0" in out
    assert "×0.37" in out
    assert "irregular" in out
    assert "per-field" in out
    assert "single-field" in out
    assert "seed=42" in out
    # The seed document's newest real stamp (acme/acme-9's second entry).
    assert "stamp base=2026-06-01T00:00:00Z" in out
