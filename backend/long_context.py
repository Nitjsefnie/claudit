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

# The default Codex meter: a request above this prompt size bills the WHOLE
# request at 2x input and 1.5x output. Applied per record by the caller,
# which is the only place that knows the request's prompt size. A model's
# threshold and optional factors can override these defaults in pricing.json.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5

# WHICH models carry the meter — pricing.json's long_context_models — is
# data (issue #471), loaded by pricing_load and re-exported through pricing
# as LONG_CONTEXT_MODELS: a new model is a data edit, never a code change,
# and the reprice pass consults it through pricing.is_long_context_model().
