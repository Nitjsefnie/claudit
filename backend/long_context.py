"""Dependency-free default long-context threshold and multipliers.

pricing re-exports these constants; consumers read through pricing so
patched thresholds reach parser imports. Import no backend module here."""
from __future__ import annotations

# The default Codex meter: a request above this prompt size bills the WHOLE
# request at 2x input and 1.5x output. Applied per record by the caller,
# which is the only place that knows the request's prompt size. A model's
# threshold and optional factors can override these defaults in pricing.json.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5

# WHICH models carry the meter — the keys in pricing.json's grouped
# long_context_meters — is data, loaded by pricing_load and
# re-exported through pricing as LONG_CONTEXT_MODELS: a new model is a data
# edit, never a code change, and the reprice pass consults it through
# pricing.is_long_context_model().
