"""The Codex long-context meter's constants, dependency-free.

pricing.py sits over the module-size baseline, whose recorded numbers
are never raised by hand — growth is fixed by moving code into a new
module — so the meter's constants live here and pricing re-exports
them. Consumers keep reading `pricing.LONG_CONTEXT_*`: that indirection
is what lets the tests monkeypatch pricing.LONG_CONTEXT_THRESHOLD and
reach the parser through the module it imports. Like constants.py, this
module imports no other backend module.
"""
from __future__ import annotations

# A Codex request whose prompt exceeds this size bills the WHOLE request at
# the long-context meter: 2x input, 1.5x output. Ported from codexmeter.
# Applied per record by the caller, which is the only place that knows the
# request's prompt size. The rule is uniform across the family — the same
# threshold and multipliers for every GPT-5.6 and GPT-6 model
# (https://developers.openai.com/api/docs/pricing, checked 2026-09-26) —
# so these stay global constants rather than per-model data in pricing.json.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5

# The canonical labels every Codex record's model carries
# (parse_codex._CODEX_MODEL_MAP) — the models whose published pricing carries
# the meter: uniform across the family (272k threshold, 2x input side, 1.5x
# output — developers.openai.com/api/docs/pricing, checked 2026-09-26), so the
# threshold and multipliers above stay global, not per-model data. The reprice
# pass consults this set to re-derive records.long_context from stored columns;
# a row of any other model never bills the meter, so its stored flag is left
# exactly as parse stored it. Keep in lockstep with parse_codex._CODEX_MODEL_MAP
# (a test pins the pairing).
LONG_CONTEXT_MODELS = frozenset({
    "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
})
