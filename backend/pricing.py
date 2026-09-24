"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in claudit. Mirrored by src/parser.js
(SV-PARSER-SPEC) — keep both in lockstep. Bump PARSER_VERSION when this
table changes; every session reparses.

Cache writes are split by TTL:
  5m write = 1.25x base input (column 'create_5m')
  1h write = 2x base input    (column 'create_1h')

Tokens recorded as cache_creation_input_tokens with NO ephemeral_5m/1h
split are charged at the 1h rate: main sessions write 98.7% of their
cache at 1h and 5m is the subagent exception. See SV-COST-SPLIT in
.claude/rules/claudit-doctrine.md.

Three resolution behaviours matter, in priority order:

1. EXACT — the normalised model id matches a key in MODEL_RATES, allowing
   only a dated-snapshot or bracket suffix after it. A version suffix the
   table doesn't know (``claude-opus-4-9``) deliberately does NOT match the
   shorter ``claude-opus-4`` key: billing a future Opus at retired 15/75
   rates is a silent 3x overcount.
2. TIER — an unrecognised Claude model falls back to its family's
   current-generation rates and is reported as ``kind="tier"`` so callers
   can mark the figure estimated rather than presenting it as fact.
3. DEFAULT — anything else. Also flagged.

Before all three, a model id ending in ``:free`` or starting with
``stealth/`` (OpenRouter's free tier and preview models) prices at ZERO.
The id list churns weekly, so the match is on the id's shape rather than
an enumerated row — checked on the raw id AND its normalised form,
case-insensitively, so no spelling can dodge it, and it outranks an
exact table key (``stealth/claude-opus-4-8`` stays free). It is reported
``kind="exact"``: the zero is a deliberate price, not an estimate, so
the API must not flag it (the same reasoning as the bonsai-2-27b row).

Rates are a function of (model, timestamp): a model may carry dated
overrides (e.g. an introductory price). Cost must be computed against the
timestamp of the request being priced, not the time of rendering.

A record that names its serving provider (OpenRouter's
``message.provider``) is priced from PROVIDER_RATES, keyed by
(normalised model, provider), when that pair has a row; otherwise, and
always when the provider is absent, by the model alone as above. A record
with no provider therefore prices exactly as it did before the provider
table existed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc


# List prices. Order: most-specific first.
MODEL_RATES = {
    # bonsai-2-27b is served by a local llama.cpp (the operator's own
    # hardware), so there is no price. Listed rather than left to the
    # DEFAULT fallback, which would bill a free lane at Opus list.
    "bonsai-2-27b":      {"fresh": 0.00,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.00,  "output": 0.00},
    # GLM (Z.ai): cache WRITES are free and reads are 0.2x input, so the
    # Anthropic 1.25x/2x/0.1x relations do not hold; explicit numbers.
    "glm-5-3-flash":     {"fresh": 0.15, "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.03,  "output": 0.50},
    # Codex (OpenAI) and Kimi lanes, merged into claudit's one table from
    # codexmeter's pricing (D4/D6): every rate explicit. D4 prices a cache
    # write at ONE rate whatever TTL the record declares (or fails to
    # declare), so create_5m and create_1h carry the same value and an
    # unsplit write is billed identically. Kimi bills cache_create at a
    # flat ZERO; Codex cache writes at 1.25x uncached input, reads at 0.1x
    # -- confirmed against OpenAI's own Sol table (4 / 0.4 / 5 / 20) and
    # Astra table (10 / 1 / 12.50 / 50). GPT-6 Sol and Luna are not in
    # codexmeter (D6). Keys are written in _normalise form (dots folded to
    # dashes): a record's "gpt-5.6-sol" normalises to "gpt-5-6-sol" before
    # matching.
    "kimi-k3":           {"fresh": 3.00,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.30,  "output": 15.00},
    "kimi-k2-7-code":    {"fresh": 0.95,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.19,  "output": 4.00},
    "kimi-k2-6":         {"fresh": 0.95,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.16,  "output": 4.00},
    "gpt-6-astra":       {"fresh": 10.00, "create_5m": 12.50, "create_1h": 12.50, "read": 1.00,  "output": 50.00},
    "gpt-6-sol":         {"fresh": 2.00,  "create_5m": 2.50,  "create_1h": 2.50,  "read": 0.20,  "output": 10.00},
    "gpt-6-luna":        {"fresh": 0.10,  "create_5m": 0.125, "create_1h": 0.125, "read": 0.01,  "output": 0.50},
    "gpt-5-6-sol":       {"fresh": 4.00,  "create_5m": 5.00,  "create_1h": 5.00,  "read": 0.40,  "output": 20.00},
    "gpt-5-6-terra":     {"fresh": 2.00,  "create_5m": 2.50,  "create_1h": 2.50,  "read": 0.20,  "output": 12.00},
    "gpt-5-6-luna":      {"fresh": 0.20,  "create_5m": 0.25,  "create_1h": 0.25,  "read": 0.02,  "output": 1.20},
    # Fable 5.1 / Mythos 5.1 price cache HITS at 0.025x base input, not the
    # 0.1x every other model uses — reads are 0.25, a quarter of Fable 5's.
    "claude-fable-5-1":  {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 0.25, "output": 50.00},
    "claude-mythos-5-1": {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 0.25, "output": 50.00},
    "claude-fable-5":    {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 1.00, "output": 50.00},
    "claude-mythos-5":   {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 1.00, "output": 50.00},
    # Opus 5.5 prices cache HITS at 0.05x base input (0.20 on a 4.00 base).
    "claude-opus-5-5":   {"fresh": 4.00,  "create_5m": 5.00,  "create_1h": 8.00,  "read": 0.20, "output": 20.00},
    "claude-opus-5":     {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-8":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-7":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-6":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-5":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-1":   {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-opus-4":     {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-sonnet-5":   {"fresh": 2.00,  "create_5m": 2.50,  "create_1h": 4.00,  "read": 0.20, "output": 10.00},
    "claude-sonnet-4-6": {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4-5": {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4":   {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-haiku-4-5":  {"fresh": 1.00,  "create_5m": 1.25,  "create_1h": 2.00,  "read": 0.10, "output": 5.00},
    "claude-3-7-sonnet-": {"fresh": 3.00, "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-3-5-sonnet-": {"fresh": 3.00, "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-3-5-haiku-": {"fresh": 0.80, "create_5m": 1.00,  "create_1h": 1.60,  "read": 0.08, "output": 4.00},
    "claude-3-opus-":    {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-3-haiku-":   {"fresh": 0.25, "create_5m": 0.30,  "create_1h": 0.50,  "read": 0.03, "output": 1.25},
}

DEFAULT_RATES = MODEL_RATES["claude-opus-4-7"]

# Every rate an OpenRouter free model carries: zero. Returned for any id
# ending in ":free" or starting with "stealth/" (see _is_free).
FREE_RATES = {k: 0.00 for k in MODEL_RATES["bonsai-2-27b"]}

# A Codex request whose prompt exceeds this size bills the WHOLE request at
# the long-context meter: 2x input, 1.5x output. Ported from codexmeter.
# Applied per record by the caller, which is the only place that knows the
# request's prompt size.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5


# Dated overrides, per exact key: (end_exclusive_utc, rates). Applied only
# when a timestamp is supplied and only on an EXACT key match — a tier
# fallback never inherits another model's promotional price. Write a window's
# rates in full, same shape as MODEL_RATES (for Anthropic models 5m = 1.25x
# input, 1h = 2x, read = 0.1x; GLM carries its own explicit numbers).
#
# GLM-5.3-Flash launch promotion: 50% off list through 2026-09-09 24:00
# UTC+8 (= 16:00 UTC). List price applies from the cutover on with no code
# change. The window stays after it expires: every PARSER_VERSION bump
# reparses the whole bucket, and a record from inside the window must
# come out at the price in force then — drop it and the next reparse
# silently reprices that history at list.
_GLM_FLASH_PROMO = {"fresh": 0.075, "create_5m": 0.00, "create_1h": 0.00, "read": 0.015, "output": 0.25}

# The two GPT-5.6 repricings, ported from codexmeter, as frozen UTC
# instants — NOT live expressions. Each is the moment of OpenAI's own
# @Product Updates post, the finest resolution available; the posts say
# "starting today" and carry no separate effective time. The source
# timestamps were read in Europe/Prague (CEST, UTC+2), so each is the
# posted wall clock minus two hours. If that reading is wrong the
# boundary moves by exactly that offset and nothing else about the
# mechanism changes.
JUL30_CUT = datetime(2026, 7, 30, 18, 12, tzinfo=UTC)   # 20:12 Europe/Prague
AUG21_CUT = datetime(2026, 8, 21, 19, 40, tzinfo=UTC)   # 21:40 Europe/Prague

# The GPT-5.6 family repriced twice, and each cut moved a different subset:
#   2026-07-09  GA                sol 5/30     terra 2.50/15   luna 1/6
#   2026-07-30  luna -80%, terra -20%          sol untouched
#   2026-08-21  sol -20% in / -33% out         terra and luna untouched
# Sol's cut is promotional, announced as running at least through
# 2026-11-21. Nothing is encoded for that: a reversion that has not happened
# is not a rate, and guessing one would silently overbill every record after
# the guessed date. Add a window when it actually moves.
#
# Ordered oldest-first, and _dated returns the FIRST window the timestamp
# falls before, so a key may carry several.
DATED_RATES: dict[str, list[tuple[datetime, dict]]] = {
    "glm-5-3-flash": [
        (datetime(2026, 9, 9, 16, 0, tzinfo=UTC), _GLM_FLASH_PROMO),
    ],
    "gpt-5-6-sol": [
        (AUG21_CUT, {"fresh": 5.00, "create_5m": 6.25, "create_1h": 6.25,
                     "read": 0.50, "output": 30.00}),
    ],
    "gpt-5-6-terra": [
        (JUL30_CUT, {"fresh": 2.50, "create_5m": 3.125, "create_1h": 3.125,
                     "read": 0.25, "output": 15.00}),
    ],
    "gpt-5-6-luna": [
        (JUL30_CUT, {"fresh": 1.00, "create_5m": 1.25, "create_1h": 1.25,
                     "read": 0.10, "output": 6.00}),
    ],
}


def _endpoint(fresh: float, read: float, output: float) -> dict:
    """One OpenRouter endpoint's rates in MODEL_RATES shape.

    Cache writes take the endpoint's cache_write price when it is nonzero
    and the input rate otherwise. Every endpoint in the snapshot lists 0 (no
    separate write price, not a free write), so every row here writes at
    the input rate, in both create buckets.
    """
    return {"fresh": fresh, "create_5m": fresh, "create_1h": fresh,
            "read": read, "output": output}


# Per-provider rates, keyed by (normalised model id, provider). The provider
# is OpenRouter's provider_name, spelled as the transcript's
# message.provider spells it ("Novita", "Morph", "Stealth").
#
# Seeded from OpenRouter's endpoints API (/api/v1/models/<author>/<slug>/
# endpoints), fetched PROVIDER_RATES_FETCHED. Figures are the prices in force
# then, with the host's promotional discount already applied; a trailing
# "N% off" records that discount. No discount carries a published end date,
# so none is encoded: a reversion that has not happened is not a rate. When
# one moves, add a PROVIDER_DATED_RATES window for the old price.
#
# A provider serving one model from two endpoints at different prices
# (Modal on glm-5.3-flash, BaseTen's cache reads on deepseek-v4.1-flash)
# carries the dearer endpoint: the transcript names only the host, and
# billing the cheaper one would under-count whenever the other served.
PROVIDER_RATES_FETCHED = datetime(2026, 9, 24, 22, 3, 13, tzinfo=UTC)
PROVIDER_RATES: dict[tuple[str, str], dict] = {
    # z-ai/glm-5.3-flash
    ("z-ai/glm-5-3-flash", "InferenceNet"): _endpoint(0.045, 0.01, 0.14),  # 50% off
    ("z-ai/glm-5-3-flash", "Sail Research"): _endpoint(0.045, 0.0285, 0.6),
    ("z-ai/glm-5-3-flash", "Relace"): _endpoint(0.07, 0.02, 0.28),
    ("z-ai/glm-5-3-flash", "DeepInfra"): _endpoint(0.075, 0.015, 0.25),  # 50% off
    ("z-ai/glm-5-3-flash", "Wafer"): _endpoint(0.089, 0.03, 0.35),
    ("z-ai/glm-5-3-flash", "GMICloud"): _endpoint(0.09, 0.018, 0.3),  # 40% off
    ("z-ai/glm-5-3-flash", "Morph"): _endpoint(0.098, 0.0196, 0.343),  # 2% off
    ("z-ai/glm-5-3-flash", "OpenInference"): _endpoint(0.1, 0.025, 0.5),
    ("z-ai/glm-5-3-flash", "Decart"): _endpoint(0.1275, 0.0255, 0.425),  # 15% off
    ("z-ai/glm-5-3-flash", "Phala"): _endpoint(0.1275, 0.0255, 0.425),  # 15% off
    ("z-ai/glm-5-3-flash", "Novita"): _endpoint(0.132, 0.0264, 0.44),  # 12% off
    ("z-ai/glm-5-3-flash", "StreamLake"): _endpoint(0.141, 0.0282, 0.47),  # 6% off
    ("z-ai/glm-5-3-flash", "Io Net"): _endpoint(0.1425, 0.0285, 0.475),  # 5% off
    ("z-ai/glm-5-3-flash", "AtlasCloud"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "BaseTen"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "CoreWeave"): _endpoint(0.15, 0.05, 0.5),
    ("z-ai/glm-5-3-flash", "Crusoe"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "DigitalOcean"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Fireworks"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Friendli"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Inceptron"): _endpoint(0.15, 0.07, 0.5),
    ("z-ai/glm-5-3-flash", "Modal"): _endpoint(0.45, 0.09, 1.5),
    ("z-ai/glm-5-3-flash", "Near AI"): _endpoint(0.15, 0.035, 0.5),
    ("z-ai/glm-5-3-flash", "Parasail"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Reka"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "SiliconFlow"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Together"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Venice"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "Z.AI"): _endpoint(0.15, 0.03, 0.5),
    ("z-ai/glm-5-3-flash", "NextBit"): _endpoint(0.165, 0.033, 0.55),
    ("z-ai/glm-5-3-flash", "Cloudflare"): _endpoint(0.3, 0.03, 1.0),
    # deepseek/deepseek-v4.1-flash
    ("deepseek/deepseek-v4-1-flash", "DekaLLM"): _endpoint(0.04, 0.01, 1.0),
    ("deepseek/deepseek-v4-1-flash", "Morph"): _endpoint(0.075, 0.0015, 0.3),  # 50% off
    ("deepseek/deepseek-v4-1-flash", "OpenInference"): _endpoint(0.1, 0.01, 0.5),
    ("deepseek/deepseek-v4-1-flash", "Relace"): _endpoint(0.1, 0.01, 0.5),
    ("deepseek/deepseek-v4-1-flash", "Sail Research"): _endpoint(0.13, 0.01, 0.75),
    ("deepseek/deepseek-v4-1-flash", "DeepInfra"): _endpoint(0.14, 0.0042, 0.42),  # 30% off
    ("deepseek/deepseek-v4-1-flash", "Alibaba"): _endpoint(0.15, 0.015, 0.6),
    ("deepseek/deepseek-v4-1-flash", "DeepSeek"): _endpoint(0.15, 0.003, 0.6),
    ("deepseek/deepseek-v4-1-flash", "StreamLake"): _endpoint(0.165, 0.0033, 0.66),  # 45% off
    ("deepseek/deepseek-v4-1-flash", "CoreWeave"): _endpoint(0.2, 0.03, 0.65),
    ("deepseek/deepseek-v4-1-flash", "Wafer"): _endpoint(0.2, 0.006, 0.6),
    ("deepseek/deepseek-v4-1-flash", "Fireworks"): _endpoint(0.22, 0.007, 0.66),
    ("deepseek/deepseek-v4-1-flash", "GMICloud"): _endpoint(0.225, 0.0045, 0.9),  # 25% off
    ("deepseek/deepseek-v4-1-flash", "Krea"): _endpoint(0.225, 0.006, 0.9),
    ("deepseek/deepseek-v4-1-flash", "Phala"): _endpoint(0.276, 0.00552, 1.104),  # 20% off
    ("deepseek/deepseek-v4-1-flash", "Novita"): _endpoint(0.285, 0.0057, 1.14),  # 5% off
    ("deepseek/deepseek-v4-1-flash", "AtlasCloud"): _endpoint(0.3, 0.03, 1.2),
    ("deepseek/deepseek-v4-1-flash", "BaseTen"): _endpoint(0.3, 0.03, 1.2),
    ("deepseek/deepseek-v4-1-flash", "DigitalOcean"): _endpoint(0.3, 0.006, 1.2),
    ("deepseek/deepseek-v4-1-flash", "Makora"): _endpoint(0.3, 0.006, 1.2),
    ("deepseek/deepseek-v4-1-flash", "Modal"): _endpoint(0.3, 0.03, 1.2),
    ("deepseek/deepseek-v4-1-flash", "NextBit"): _endpoint(0.3, 0.006, 1.2),
    ("deepseek/deepseek-v4-1-flash", "Parasail"): _endpoint(0.3, 0.006, 1.2),
    ("deepseek/deepseek-v4-1-flash", "SiliconFlow"): _endpoint(0.3, 0.006, 1.2),
    ("deepseek/deepseek-v4-1-flash", "Together"): _endpoint(0.3, 0.006, 1.2),
    ("deepseek/deepseek-v4-1-flash", "Venice"): _endpoint(0.375, 0.0075, 1.5),
    # stealth/space-bunny-alpha
    ("stealth/space-bunny-alpha", "Stealth"): _endpoint(0.0, 0.0, 0.0),
    # deepseek/deepseek-v4-flash-0731
    ("deepseek/deepseek-v4-flash-0731", "Relace"): _endpoint(0.03, 0.016, 0.32),
    ("deepseek/deepseek-v4-flash-0731", "Sail Research"): _endpoint(0.038, 0.0228, 0.55),
    ("deepseek/deepseek-v4-flash-0731", "StreamLake"): _endpoint(0.0528, 0.00168, 0.1584),  # 88% off
    ("deepseek/deepseek-v4-flash-0731", "DeepInfra"): _endpoint(0.06, 0.015, 0.18),
    ("deepseek/deepseek-v4-flash-0731", "Wafer"): _endpoint(0.08, 0.02, 0.35),
    ("deepseek/deepseek-v4-flash-0731", "Inceptron"): _endpoint(0.0828, 0.06, 0.4138),
    ("deepseek/deepseek-v4-flash-0731", "Reka"): _endpoint(0.088, 0.0056, 0.528),  # 20% off
    ("deepseek/deepseek-v4-flash-0731", "Makora"): _endpoint(0.09, 0.0196, 0.195),
    ("deepseek/deepseek-v4-flash-0731", "DigitalOcean"): _endpoint(0.119, 0.0238, 0.238),
    ("deepseek/deepseek-v4-flash-0731", "BaseTen"): _endpoint(0.13, 0.028, 0.26),
    ("deepseek/deepseek-v4-flash-0731", "CoreWeave"): _endpoint(0.13, 0.07, 0.28),
    ("deepseek/deepseek-v4-flash-0731", "Cohere"): _endpoint(0.14, 0.07, 0.28),
    ("deepseek/deepseek-v4-flash-0731", "Nebius"): _endpoint(0.14, 0.0, 0.28),
    ("deepseek/deepseek-v4-flash-0731", "OpenInference"): _endpoint(0.14, 0.03, 0.7),
    ("deepseek/deepseek-v4-flash-0731", "Parasail"): _endpoint(0.14, 0.05, 0.28),
    ("deepseek/deepseek-v4-flash-0731", "Together"): _endpoint(0.14, 0.03, 0.28),
    ("deepseek/deepseek-v4-flash-0731", "Morph"): _endpoint(0.141953, 0.035937, 0.399625),
    ("deepseek/deepseek-v4-flash-0731", "Venice"): _endpoint(0.175, 0.035, 0.35),
    ("deepseek/deepseek-v4-flash-0731", "Alibaba"): _endpoint(0.176, 0.0176, 0.528),
    ("deepseek/deepseek-v4-flash-0731", "Mancer 2"): _endpoint(0.2, 0.0, 0.6),
    ("deepseek/deepseek-v4-flash-0731", "Fireworks"): _endpoint(0.22, 0.007, 0.66),
    ("deepseek/deepseek-v4-flash-0731", "SiliconFlow"): _endpoint(0.22, 0.028, 0.66),
    ("deepseek/deepseek-v4-flash-0731", "GMICloud"): _endpoint(0.286, 0.0091, 0.858),  # 35% off
    ("deepseek/deepseek-v4-flash-0731", "Phala"): _endpoint(0.308, 0.0196, 0.924),  # 30% off
    ("deepseek/deepseek-v4-flash-0731", "NextBit"): _endpoint(0.352, 0.012, 1.056),
    ("deepseek/deepseek-v4-flash-0731", "Novita"): _endpoint(0.4092, 0.02604, 1.2276),  # 7% off
    ("deepseek/deepseek-v4-flash-0731", "AtlasCloud"): _endpoint(0.44, 0.028, 1.32),
    ("deepseek/deepseek-v4-flash-0731", "Baidu"): _endpoint(0.44, 0.014, 1.32),
    ("deepseek/deepseek-v4-flash-0731", "Cloudflare"): _endpoint(0.44, 0.014, 1.32),
    # deepseek/deepseek-v4-flash
    ("deepseek/deepseek-v4-flash", "Relace"): _endpoint(0.05, 0.01, 0.25),
    ("deepseek/deepseek-v4-flash", "StreamLake"): _endpoint(0.06398, 0.012796, 0.12796),  # 54% off
    ("deepseek/deepseek-v4-flash", "Baidu"): _endpoint(0.06538, 0.013076, 0.13076),  # 53% off
    ("deepseek/deepseek-v4-flash", "DeepInfra"): _endpoint(0.09, 0.018, 0.18),
    ("deepseek/deepseek-v4-flash", "GMICloud"): _endpoint(0.091, 0.0182, 0.182),  # 35% off
    ("deepseek/deepseek-v4-flash", "Venice"): _endpoint(0.0966, 0.0196, 0.1925),  # 30% off
    ("deepseek/deepseek-v4-flash", "DigitalOcean"): _endpoint(0.098, 0.0196, 0.196),
    ("deepseek/deepseek-v4-flash", "SiliconFlow"): _endpoint(0.13, 0.028, 0.28),
    ("deepseek/deepseek-v4-flash", "Alibaba"): _endpoint(0.134, 0.0268, 0.268),
    ("deepseek/deepseek-v4-flash", "AtlasCloud"): _endpoint(0.14, 0.028, 0.28),
    ("deepseek/deepseek-v4-flash", "Novita"): _endpoint(0.14, 0.028, 0.28),
    ("deepseek/deepseek-v4-flash", "OpenInference"): _endpoint(0.14, 0.03, 0.7),
    ("deepseek/deepseek-v4-flash", "Parasail"): _endpoint(0.14, 0.07, 0.28),
    ("deepseek/deepseek-v4-flash", "NextBit"): _endpoint(0.15, 0.035, 0.3),
    ("deepseek/deepseek-v4-flash", "Mancer 2"): _endpoint(0.19, 0.0, 0.5),
    ("deepseek/deepseek-v4-flash", "Azure"): _endpoint(0.21, 0.031, 0.56),
}

# Dated overrides per (model, provider) row, same shape and semantics as
# DATED_RATES. Empty: no provider price has moved since the snapshot.
PROVIDER_DATED_RATES: dict[tuple[str, str], list[tuple[datetime, dict]]] = {}

# Sorted boundaries where any rate changes. Read-time aggregation that
# re-derives rates from summed tokens must group by these, or its
# per-component breakdown drifts from the stored per-record cost.
RATE_EPOCHS: list[datetime] = sorted(
    {end for windows in DATED_RATES.values() for end, _ in windows}
    | {end for windows in PROVIDER_DATED_RATES.values() for end, _ in windows}
)

_VERSIONED_KEY = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d+)*)$")


def _latest(*families: str) -> dict:
    """Rates of the highest-versioned table key in `families`.

    ``claude-opus-5-5`` is version (5, 5); legacy ``claude-3-opus-`` keys
    do not match. Ties keep table order (max returns the first).
    """
    versions = [
        (tuple(int(p) for p in m.group(2).split("-")), key)
        for key in MODEL_RATES
        if (m := _VERSIONED_KEY.match(key)) and m.group(1) in families
    ]
    return MODEL_RATES[max(versions, key=lambda v: v[0])[1]]


# Family fallbacks for unrecognised Claude models — current-generation
# rates for the tier, at LIST price (never a dated promotion). Derived
# from the table, so adding a newer model moves its family's fallback.
_TIER_FALLBACKS: tuple[tuple[re.Pattern, dict], ...] = (
    (re.compile(r"fable|mythos"), _latest("fable", "mythos")),
    (re.compile(r"opus"), _latest("opus")),
    (re.compile(r"sonnet"), _latest("sonnet")),
    (re.compile(r"haiku"), _latest("haiku")),
)

# A dated snapshot suffix ("-20250514") is the same model; a short version
# suffix ("-9") or a mode suffix ("-fast") is a DIFFERENT model.
_SNAPSHOT_SUFFIX = re.compile(r"^-?\d{6,8}$")


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a model id to rates.

    kind: "exact" | "tier" | "default". Anything other than "exact" means
    the figure is an estimate and should be surfaced as such.
    """
    rates: dict
    kind: str
    key: str | None = None

    @property
    def estimated(self) -> bool:
        return self.kind != "exact"


def _normalise(model: str) -> str:
    """Strip provider/region prefixes and normalise version separators.

    ``anthropic/claude-opus-4.8`` and ``us.anthropic.claude-opus-4-8``
    both denote the same model as ``claude-opus-4-8``.
    """
    m = (model or "").strip().lower()
    if not m:
        return ""
    # Everything before the first "claude" is provider/region routing.
    i = m.find("claude")
    if i > 0:
        m = m[i:]
    return m.replace(".", "-")


def _is_free(model: str, norm: str) -> bool:
    """True for an OpenRouter free model: an id ending in ``:free`` or
    starting with ``stealth/``, case-insensitively.

    Checked on the raw id as well as its normalised form because
    ``_normalise`` strips everything before ``claude`` — a
    ``stealth/claude-…`` id loses that prefix in ``norm`` and only the
    raw check still sees it. (The ``:free`` suffix survives every
    normalisation step; the raw check covers it symmetrically.)
    """
    raw = (model or "").strip().lower()
    return (norm.endswith(":free") or norm.startswith("stealth/")
            or raw.endswith(":free") or raw.startswith("stealth/"))


def _match_key(norm: str) -> str | None:
    for key in MODEL_RATES:
        if not norm.startswith(key):
            continue
        rest = norm[len(key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            return key
    return None


def _in_window(windows: list | None, ts: datetime | None,
               list_rates: dict) -> dict:
    if not windows or ts is None:
        return list_rates
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for end_exclusive, rates in windows:
        if ts < end_exclusive:
            return rates
    return list_rates


def _dated(key: str, ts: datetime | None) -> dict:
    return _in_window(DATED_RATES.get(key), ts, MODEL_RATES[key])


# OpenRouter's dated permaslug ("deepseek/deepseek-v4-flash-20260731") names
# the same model as its short slug ("deepseek/deepseek-v4-flash-0731").
_PERMASLUG_DATE = re.compile(r"-20\d{2}(\d{4})$")


def _provider_key(norm: str, provider: str) -> tuple[str, str] | None:
    """The PROVIDER_RATES key for a record, or None.

    Exact on the normalised id, or on its permaslug folded to the slug.
    Never MODEL_RATES' snapshot-suffix tolerance: that would read the
    permaslug as the UNDATED model, a different row at a different price.
    """
    for model in (norm, _PERMASLUG_DATE.sub(r"-\1", norm)):
        if (model, provider) in PROVIDER_RATES:
            return model, provider
    return None


def resolve(model: str, ts: datetime | None = None,
            provider: str | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is.

    `provider` is the record's serving host. A (model, provider) row wins;
    with no row, or no provider, the model alone decides.
    """
    norm = _normalise(model)
    if _is_free(model, norm):
        return Resolution(FREE_RATES, "exact", norm)
    pkey = _provider_key(norm, provider) if provider else None
    if pkey is not None:
        return Resolution(
            _in_window(PROVIDER_DATED_RATES.get(pkey), ts, PROVIDER_RATES[pkey]),
            "exact", pkey[0])
    key = _match_key(norm)
    if key is not None:
        return Resolution(_dated(key, ts), "exact", key)
    for pattern, rates in _TIER_FALLBACKS:
        if pattern.search(norm):
            return Resolution(rates, "tier")
    return Resolution(DEFAULT_RATES, "default")


def rate_for(model: str, ts: datetime | None = None,
             provider: str | None = None) -> dict:
    """Rates for a model at a point in time. Omitting ts yields list price."""
    return resolve(model, ts, provider).rates


def compute_cost(
    model: str,
    *,
    fresh: int,
    output: int,
    eph5: int,
    eph1h: int,
    unsplit_create: int,
    read: int,
    ts: datetime | None = None,
    long_context: bool = False,
    provider: str | None = None,
) -> float:
    """USD cost for one request's token tally.

    unsplit_create = max(0, cache_creation_input_tokens - eph5 - eph1h);
    must already be computed by the caller. Pass the record's own
    timestamp so dated rates apply to when the tokens were spent.

    A write with no declared TTL is priced as 1h: main sessions write
    98.7% of their cache at 1h, and 5m is the subagent exception (96% of
    all 5m writes). See SV-COST-SPLIT.

    long_context applies the Codex long-context meter (2x input side,
    1.5x output) to the whole request. It defaults off, so every existing
    caller is unaffected: no Kimi caller passes it (the wire format has no
    such tier), and neither does a Codex record on a subscription.

    provider is the record's serving host (OpenRouter's message.provider);
    None prices by the model alone, exactly as before the provider table.
    """
    r = rate_for(model, ts, provider)
    in_mult = LONG_CONTEXT_INPUT_MULT if long_context else 1.0
    out_mult = LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + eph5 * r["create_5m"] * in_mult / 1_000_000
        + (eph1h + unsplit_create) * r["create_1h"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
    )
