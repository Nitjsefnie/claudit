# Merge kimimeter and codexmeter into claudit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One claudit codebase that ingests Claude, Codex and Kimi transcripts from one or several R2 buckets, with its branding taken from config, so codexmeter and kimimeter become claudit deploys and their repos retire.

**Architecture:** claudit hosts everything below the parser (schema, rollups, panels, CI, deploys). codexmeter donates its parser layer, which already dispatches between three formats over one shared state machine (`parse_common.py`); claudit's Claude walker becomes a fourth format behind the same dispatcher. An adapter normalises every format's rows to claudit's record and tool_use shape. kimimeter contributes nothing that codexmeter does not already carry, so it needs no port of its own.

**Tech Stack:** Python 3.13, FastAPI, psycopg3, Postgres, in-browser React/Babel, pytest, node (for the JS mirror tests).

**Spec:** the Decisions section below, settled in session `31d86e35` on 2026-09-22 from two divergence surveys and measurements over the three production databases. The surveys are summarised under "Evidence".

## Global Constraints

- claudit is the host; `/root/codexmeter` and `/root/kimimeter` are READ-ONLY sources. Copy from them; never edit, commit or push in them.
- Work on branch `merge-lane-meters` in the worktree `/tmp/claudit-merge-lane-meters`. Never touch `/root/claudit`: it is the live claudit deploy checkout, on `master`. Never commit to `master`.
- `constants.PARSER_VERSION` stays a code constant (SV-PARSER-VERSION). Bump it once in every task that changes parse or pricing output, with a one-line comment in `backend/constants.py` above it.
- A cache write with no declared TTL is priced at the 1h rate (SV-COST-SPLIT, since `c1608e0`).
- `records.thinking_tokens` is the one subset-of-output column. Codex's `reasoning_output_tokens` is written into it; never add a second column for the same concept (SV-SUBSET-TOKENS).
- Every schema change is additive: `ADD COLUMN IF NOT EXISTS`, nullable or defaulted (SV-SCHEMA-AUTOAPPLY).
- A bucket list is written `R2_BUCKET=a+b+c`. `+` is the separator: it cannot occur in an R2 bucket name, and unlike `|` it is not a shell operator when an `.env` is sourced.
- Fixture files under `fixtures/parser/` stay under 1 KB each (SV-FIXTURE-SIZE).
- Before every commit run, from `/tmp/claudit-merge-lane-meters`, and fix anything they report. The venv lives in the deploy checkout; use it by absolute path (`V=/root/claudit/.venv/bin`) and never modify it:
  ```
  $V/python -m pytest tests/ -q --tb=short -p no:warnings
  git ls-files -co --exclude-standard '*.py' | xargs $V/pylint
  git ls-files -co --exclude-standard '*.py' | xargs $V/pycodestyle
  $V/pyright --pythonpath $V/python
  npx --prefix /root/claudit --no-install eslint 'src/**/*.js' 'src/**/*.jsx'
  $V/python scripts/ci/smoke.py
  ```
- A new file must be visible to git: `git check-ignore -v <path>` must print nothing. `.gitignore` is deny-by-default; add a name-back rule in the file's own directory block if needed, never loosen the leading `*`.
- Stage explicit paths, never `git add -A`. End every commit message with `Co-Authored-By: <your own serving model's plain name> <its vendor's noreply address>`.

---

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | claudit is the host; codexmeter's parser layer is lifted in | claudit has 5 more rollups, the agent/context/cost panels, `suppressed_models`, turn_flags, read analysis, 10 CI workflows and 3 live deploys. codexmeter already has the multi-format parser claudit lacks. |
| D2 | kimimeter retires with no port | Its two formats (legacy, kimi-code) are already in codexmeter's `parse.py`; it has no table, column, endpoint or panel claudit lacks. |
| D3 | `reasoning_output_tokens` → `thinking_tokens` | Same quantity under another provider's name: a subset of output, never priced separately. |
| D4 | Flat-create models store the same rate in `create_5m` and `create_1h` | Codex and Kimi have one cache-write rate with no TTL. Measured: neither lane has ever recorded a write (Codex reports `cache_write_input_tokens` in 9,570 of 9,570 usage blocks, always 0; 0 across 351,864 corpus records; Kimi 0 across 61,728). The path is dormant but must price correctly if it fires. |
| D5 | `compute_cost` gains `long_context: bool = False` | Codex bills a request whose prompt exceeds 272,000 tokens at 2× input-side and 1.5× output rates. |
| D6 | GPT-6 Sol and Luna are priced from OpenAI's published table (2026-09-22) | Sol 2.00 / 0.20 / 2.50 / 10.00, Luna 0.10 / 0.01 / 0.125 / 0.50 (input / cached / cache write / output, USD per 1M). Confirmed by the user from the official page. |
| D7 | The Codex model map lists `gpt-6-sol` and `gpt-6-luna` before the bare `sol` / `luna` needles | It matches substrings in order; without that, a GPT-6 Sol record is relabelled GPT-5.6 Sol and billed at twice the price. |
| D8 | Stored `files.file_key` becomes `<bucket>/<object-key>` in every deploy | Once a deploy reads several buckets, a key alone no longer identifies a file: today the archiver moved objects with identical keys from `claude` to `llama`. A bucket name cannot contain `/`, so the first segment is unambiguous. One scheme for single- and multi-bucket deploys avoids two code paths. |
| D9 | Branding comes from `APP_NAME`, `APP_TITLE`, `APP_DESCRIPTION` | Injected at the existing `index.html` rewrite point as `window.BRAND`; the FastAPI title and PNG filename read `APP_NAME`. Defaults reproduce today's claudit strings. |
| D10 | The env flags keep their `CLAUDIT_` prefix | The merged app is claudit; codexmeter and kimimeter deploys set `CLAUDIT_*` like glmmeter and llamameter already do. |

## Evidence

- **codexmeter** (`/root/codexmeter`, HEAD `4e6b14e`): `backend/parse_common.py` (264 lines, "Nothing here knows a format"), `backend/parse_codex.py` (642), `backend/parse.py` (576) holding both Kimi parsers and the dispatcher `parse_file` at line 511, which recognises Claude transcripts and raises `UnsupportedTranscriptError`.
- **kimimeter** (`/root/kimimeter`, HEAD `968c478`): a strict subset of codexmeter's formats and of claudit's features.
- **Lane key layout**: both lanes write `sessions/<project>/<session>/wire.jsonl.xz`, subagents under `.../<session>/subagents/<id>/wire.jsonl.xz`, and a `sessions/<project>/project.json` marker carrying the project's display path. codexmeter maps project = segment 1, session = segment 2, is_main = no `/subagents/` segment (`ingest.py:198-224`).
- **Parse output**: codexmeter's `_finish_parse` returns `records, ctx_turns, turn_count, rate_limit_hits, tool_uses`; its tool_uses carry `tool_call_id`, `model`, `is_error=None`. claudit's `_persist` (`backend/ingest.py:639`) additionally reads `prompt_count`, `models`, `agent_type` and inserts the tool_use columns listed in Task 2.

---

### Task 1: Merged pricing — every lane's models in one table

**Files:**
- Modify: `backend/pricing.py` (`MODEL_RATES`, `DATED_RATES`, `compute_cost`)
- Modify: `src/parser.js` (`window.modelRates`, `window.datedRates` mirror)
- Modify: `backend/constants.py` (`PARSER_VERSION`)
- Create: `tests/test_pricing_lanes.py`

**Interfaces:**
- Produces: `pricing.compute_cost(model, *, fresh, output, eph5, eph1h, unsplit_create, read, ts=None, long_context=False) -> float`
- Produces: `pricing.LONG_CONTEXT_THRESHOLD = 272_000`, `LONG_CONTEXT_INPUT_MULT = 2.0`, `LONG_CONTEXT_OUTPUT_MULT = 1.5`
- Produces: `MODEL_RATES` keys `kimi-k3`, `kimi-k2-7-code`, `kimi-k2-6`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, each resolving `exact`

- [ ] **Step 1: Write the failing tests** in `tests/test_pricing_lanes.py`:

```python
"""Codex and Kimi models priced by claudit's table (D4, D5, D6)."""
import pytest

from backend import pricing

# (fresh, cache write, cached read, output), USD per 1M tokens.
LIST = {
    "kimi-k3":        (3.00, 0.00, 0.30, 15.00),
    "kimi-k2-7-code": (0.95, 0.00, 0.19, 4.00),
    "kimi-k2-6":      (0.95, 0.00, 0.16, 4.00),
    "gpt-6-astra":    (10.00, 12.50, 1.00, 50.00),
    "gpt-6-sol":      (2.00, 2.50, 0.20, 10.00),
    "gpt-6-luna":     (0.10, 0.125, 0.01, 0.50),
    "gpt-5.6-sol":    (4.00, 5.00, 0.40, 20.00),
    "gpt-5.6-terra":  (2.00, 2.50, 0.20, 12.00),
    "gpt-5.6-luna":   (0.20, 0.25, 0.02, 1.20),
}


@pytest.mark.parametrize("model", sorted(LIST))
def test_lane_model_resolves_exact_at_its_list_rate(model):
    fresh, create, read, output = LIST[model]
    r = pricing.resolve(model)
    assert r.kind == "exact"
    assert r.rates["fresh"] == fresh
    assert r.rates["read"] == read
    assert r.rates["output"] == output
    # D4: one write rate, whatever TTL the record does or does not declare.
    assert r.rates["create_5m"] == r.rates["create_1h"] == create


def test_flat_create_prices_identically_under_any_declared_ttl():
    kw = dict(fresh=0, output=0, read=0)
    as_5m = pricing.compute_cost("gpt-6-sol", eph5=1_000_000, eph1h=0, unsplit_create=0, **kw)
    as_1h = pricing.compute_cost("gpt-6-sol", eph5=0, eph1h=1_000_000, unsplit_create=0, **kw)
    undeclared = pricing.compute_cost("gpt-6-sol", eph5=0, eph1h=0, unsplit_create=1_000_000, **kw)
    assert as_5m == as_1h == undeclared == pytest.approx(2.50)


def test_long_context_doubles_input_side_and_raises_output_by_half():
    kw = dict(fresh=1_000_000, output=1_000_000, eph5=0, eph1h=0,
              unsplit_create=1_000_000, read=1_000_000)
    base = pricing.compute_cost("gpt-6-sol", **kw)
    long = pricing.compute_cost("gpt-6-sol", long_context=True, **kw)
    assert base == pytest.approx(2.00 + 10.00 + 2.50 + 0.20)
    assert long == pytest.approx(2 * (2.00 + 2.50 + 0.20) + 1.5 * 10.00)


def test_long_context_defaults_off_for_every_existing_caller():
    kw = dict(fresh=1_000_000, output=0, eph5=0, eph1h=0, unsplit_create=0, read=0)
    assert pricing.compute_cost("claude-opus-5", **kw) == pricing.compute_cost(
        "claude-opus-5", long_context=False, **kw)


@pytest.mark.parametrize("model,before,rates", [
    # Windows copied from codexmeter backend/pricing.py DATED_RATES.
    ("gpt-5.6-sol", "2026-08-21T19:39:59+00:00", (5.00, 6.25, 0.50, 30.00)),
    ("gpt-5.6-terra", "2026-07-30T18:11:59+00:00", (2.50, 3.125, 0.25, 15.00)),
    ("gpt-5.6-luna", "2026-07-30T18:11:59+00:00", (1.00, 1.25, 0.10, 6.00)),
])
def test_gpt56_dated_windows_survive_the_port(model, before, rates):
    from datetime import datetime
    r = pricing.rate_for(model, datetime.fromisoformat(before))
    fresh, create, read, output = rates
    assert (r["fresh"], r["create_5m"], r["create_1h"], r["read"], r["output"]) == (
        fresh, create, create, read, output)
```

- [ ] **Step 2: Run to verify they fail**

Run: `$V/python -m pytest tests/test_pricing_lanes.py -q --tb=line`
Expected: FAIL — the lane models resolve `default`, and `compute_cost` rejects `long_context`.

- [ ] **Step 3: Implement.**
  - Add the nine rows above to `MODEL_RATES` in `backend/pricing.py`, converting codexmeter's `{"fresh", "create", "read", "output"}` shape to claudit's by writing the create value into both `create_5m` and `create_1h`. Put a comment above the block citing D4 and D6.
  - Copy codexmeter's `JUL30_CUT`, `AUG21_CUT` and its three `DATED_RATES` windows (`/root/codexmeter/backend/pricing.py`, the block after `DEFAULT_RATES`), converted the same way. Keep the comments that explain each cut.
  - Add the three long-context constants and the `long_context` keyword to `compute_cost`: `in_mult` multiplies fresh, both create terms and read; `out_mult` multiplies output — exactly as codexmeter's `compute_cost` does.
  - Mirror the new rows and windows in `src/parser.js` (`c5`/`c1h` both set to the create value). `tests/test_parser_js_mirror.py` asserts the two tables are identical.
  - Bump `PARSER_VERSION` with the comment `# NN adds the Codex and Kimi rate tables and the long-context meter.`

- [ ] **Step 4: Run to verify they pass**, then the full gate list from Global Constraints.

- [ ] **Step 5: Commit** — `git add backend/pricing.py src/parser.js backend/constants.py tests/test_pricing_lanes.py`, message `Price the Codex and Kimi models in claudit's rate table`.

---

### Task 2: Format dispatch — Codex and Kimi parsers behind `parse_file`

**Files:**
- Create: `backend/parse_common.py` (copied from `/root/codexmeter/backend/parse_common.py`)
- Create: `backend/parse_codex.py` (copied from `/root/codexmeter/backend/parse_codex.py`, plus D7)
- Create: `backend/parse_kimi.py` (codexmeter `backend/parse.py` lines 1-476: model attribution, `_edit_churn`, the legacy and kimi-code parsers)
- Create: `backend/parse_lanes.py` (adapter to claudit's row shape)
- Modify: `backend/parse.py` (`parse_file` becomes the dispatcher; the Claude path is unchanged)
- Modify: `backend/constants.py`
- Create: `tests/test_parse_lanes.py`, `fixtures/parser/codex_min.jsonl`, `fixtures/parser/kimi_code_min.jsonl`, `fixtures/parser/kimi_legacy_min.jsonl`

**Interfaces:**
- Consumes: Task 1's `pricing.compute_cost(..., long_context=)`
- Produces: `parse.parse_file(file_key: str, blob: bytes) -> dict` with keys `records, ctx_turns, turn_count, prompt_count, models, rate_limit_hits, tool_uses, agent_type`, for every format
- Produces: `parse.sniff_format(blob: bytes) -> Literal["claude", "codex", "kimi-code", "legacy"]`
- Produces: `parse_lanes.to_claudit(parsed: dict) -> dict`

- [ ] **Step 1: Copy the modules and make them import cleanly.**
  - Copy `parse_common.py` and `parse_codex.py` verbatim, then change only what claudit's names require: every `pricing.compute_cost(model, fresh=, create=, read=, output=, long_context=, ts=)` call becomes `pricing.compute_cost(model, fresh=, output=, eph5=0, eph1h=0, unsplit_create=create, read=, long_context=, ts=)`.
  - Apply D7 to `_CODEX_MODEL_MAP` in `parse_codex.py`, with this comment:
    ```python
    # Matched as SUBSTRINGS, in order, so a more specific id must come before
    # any needle it contains: `gpt-6-sol` contains `sol`, and listed after it
    # would be relabelled GPT-5.6 Sol and billed at twice its price.
    _CODEX_MODEL_MAP = (
        ("astra", "gpt-6-astra"),
        ("gpt-6-sol", "gpt-6-sol"),
        ("gpt-6-luna", "gpt-6-luna"),
        ("terra", "gpt-5.6-terra"),
        ("luna", "gpt-5.6-luna"),
        ("sol", "gpt-5.6-sol"),
    )
    ```
  - Move codexmeter `parse.py` lines 1-476 into `parse_kimi.py`, fixing imports to `backend.parse_common`.

- [ ] **Step 2: Write the failing tests** in `tests/test_parse_lanes.py`. Build the three fixtures by taking the smallest real file of each format from the lane buckets and trimming it to one request (a session opener, one user turn, one usage record, one tool call and result), each under 1 KB.

```python
"""Codex and Kimi transcripts through claudit's parse_file (D2, D3, D7)."""
from pathlib import Path

import pytest

from backend import parse

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"
CLAUDIT_RECORD_KEYS = {
    "file_key", "line_num", "uuid", "request_id", "ts", "model",
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "output_tokens", "text_chars", "reply_latency_s", "stop_reason",
    "effort", "thinking_tokens", "cli_version", "turn_flags",
    "turn_tool_results", "eph5_tokens", "eph1h_tokens", "cost_usd",
}


@pytest.mark.parametrize("name,fmt", [
    ("codex_min.jsonl", "codex"),
    ("kimi_code_min.jsonl", "kimi-code"),
    ("kimi_legacy_min.jsonl", "legacy"),
])
def test_each_lane_format_is_sniffed(name, fmt):
    assert parse.sniff_format((FIX / name).read_bytes()) == fmt


def test_a_claude_transcript_still_sniffs_as_claude():
    assert parse.sniff_format((FIX / "unsplit_cache.jsonl").read_bytes()) == "claude"


@pytest.mark.parametrize("name", [
    "codex_min.jsonl", "kimi_code_min.jsonl", "kimi_legacy_min.jsonl"])
def test_lane_records_carry_every_column_claudit_persists(name):
    out = parse.parse_file(f"sessions/p/s/{name}", (FIX / name).read_bytes())
    assert out["records"], "fixture must hold at least one usage record"
    for r in out["records"]:
        assert CLAUDIT_RECORD_KEYS <= set(r), CLAUDIT_RECORD_KEYS - set(r)
        assert r["eph5_tokens"] == r["eph1h_tokens"] == 0
        assert r["turn_flags"] == []
    assert {"prompt_count", "models", "agent_type"} <= set(out)


def test_codex_reasoning_lands_in_thinking_tokens():
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           (FIX / "codex_min.jsonl").read_bytes())
    r = out["records"][0]
    assert 0 < r["thinking_tokens"] <= r["output_tokens"]
    assert "reasoning_output_tokens" not in r


@pytest.mark.parametrize("raw,label", [
    ("gpt-6-sol", "gpt-6-sol"), ("GPT-6-Sol", "gpt-6-sol"),
    ("gpt-6-luna", "gpt-6-luna"), ("gpt-5.6-sol", "gpt-5.6-sol"),
    ("gpt-5.6-luna", "gpt-5.6-luna"), ("gpt-5.6-terra", "gpt-5.6-terra"),
    ("gpt-6-astra", "gpt-6-astra"),
])
def test_codex_model_map_keeps_the_generations_apart(raw, label):
    from backend import parse_codex
    assert parse_codex._codex_model(raw) == label  # pylint: disable=protected-access


def test_lane_tool_uses_carry_claudit_columns():
    out = parse.parse_file("sessions/p/s/wire.jsonl",
                           (FIX / "codex_min.jsonl").read_bytes())
    tu = out["tool_uses"][0]
    for key in ("tool_use_id", "error_kind", "error_text", "agent_type",
                "agent_model", "dispatch_prompt_chars", "dispatch_brief_ref",
                "result_chars", "read_kind", "read_targets", "write_targets",
                "is_reread"):
        assert key in tu, key
    assert "tool_call_id" not in tu
```

- [ ] **Step 3: Run to verify they fail** — `sniff_format` does not exist yet.

- [ ] **Step 4: Implement** `parse_lanes.to_claudit(parsed)`: for every record, rename `reasoning_output_tokens` → `thinking_tokens`, and set `request_id=None, stop_reason=None, effort=None, cli_version=None, turn_flags=[], turn_tool_results=0, eph5_tokens=0, eph1h_tokens=0`. For every tool_use, rename `tool_call_id` → `tool_use_id`, drop `model`, and set `error_kind=None, error_text=None, agent_type=None, agent_model=None, dispatch_prompt_chars=None, dispatch_brief_ref=None, result_chars=None, read_kind=None, read_targets=None, write_targets=None, is_reread=None`. Add `prompt_count` (the count of user turns the lane parser recorded, else 0), `models` (sorted distinct record models) and `agent_type=None`.
  In `backend/parse.py`, move the current `parse_file` body to `_parse_claude(file_key, blob)` unchanged, add `sniff_format` (port codexmeter's line sniff from its `parse_file`, lines 511-576, returning `"claude"` where codexmeter raised), and make `parse_file` dispatch: `claude` → `_parse_claude`, anything else → `parse_lanes.to_claudit(<format parser>(file_key, blob))`.

- [ ] **Step 5: Port codexmeter's format tests** — `tests/test_parse_codex.py`, `tests/test_parse_codex_pricing.py` and the Kimi cases of its `tests/test_parse.py` — as `tests/test_parse_codex.py`, `tests/test_parse_codex_pricing.py`, `tests/test_parse_kimi.py`, adjusting only field names (D3) and the `compute_cost` keyword shape. A test that fails for a reason other than naming is a real regression: stop and report it.

- [ ] **Step 6: Bump `PARSER_VERSION`**, run the full gates, commit with message `Parse Codex and Kimi transcripts behind one format dispatcher`.

---

### Task 3: Ingest reads the lane key layout

**Files:**
- Create: `backend/key_layout.py`
- Modify: `backend/ingest.py` (`_collect_todo`, `_persist`, project display names)
- Create: `tests/test_key_layout.py`

**Interfaces:**
- Produces: `key_layout.classify(key: str) -> KeyInfo | None` where `KeyInfo = NamedTuple(project_id: str, session_id: str, is_main: bool)`; `None` means "not a transcript".
- Produces: `key_layout.project_marker(key: str) -> str | None` — the project id when `key` is `sessions/<project>/project.json`.

- [ ] **Step 1: Write the failing tests:**

```python
from backend.key_layout import KeyInfo, classify, project_marker


def test_claude_layout():
    assert classify("-root-claudit/abc/abc.jsonl.xz") == KeyInfo("-root-claudit", "abc", True)


def test_claude_subagent_sidecar_is_not_main():
    info = classify("-root-claudit/abc/subagents/agent-x.jsonl.xz")
    assert info is not None and info.is_main is False


def test_lane_main_session():
    assert classify("sessions/8805b8ac99ad/01a0-uuid/wire.jsonl.xz") == KeyInfo(
        "8805b8ac99ad", "01a0-uuid", True)


def test_lane_subagent():
    assert classify("sessions/aa5d/019f-parent/subagents/019f-child/wire.jsonl.xz") == KeyInfo(
        "aa5d", "019f-parent", False)


def test_non_transcripts_are_skipped():
    assert classify("user-history/2026.jsonl") is None
    assert classify("sessions/aa5d/project.json") is None


def test_project_marker():
    assert project_marker("sessions/aa5d/project.json") == "aa5d"
    assert project_marker("sessions/aa5d/x/wire.jsonl") is None
```

  Read claudit's current `_collect_todo` and `_persist` first and make `test_claude_layout` / `test_claude_subagent_sidecar_is_not_main` encode exactly what they derive today (including how claudit sets `is_main`) — adjust the expected values to match current behaviour, never the other way round.

- [ ] **Step 2: Run to verify they fail**, **Step 3: implement** `key_layout.py` and route `_collect_todo` / `_persist` through `classify`. Port codexmeter's `project.json` marker read (`_scan_r2`, `ingest.py:127-150`) so lane projects get their display path.
- [ ] **Step 4:** the existing `tests/test_ingest.py` must pass unchanged; add one ingest test over a two-file lane-layout mini mirror built in `tmp_path`.
- [ ] **Step 5:** gates, commit `Map the Codex and Kimi key layout to projects and sessions`.

---

### Task 4: Several buckets per deploy

**Files:**
- Modify: `backend/r2.py`, `backend/ingest.py`, `backend/api_sessions.py` (transcript and sidecar fetch)
- Modify: `backend/key_layout.py` (classify strips the bucket segment)
- Create: `tests/test_multi_bucket.py`

**Interfaces:**
- Produces: `r2.buckets() -> list[str]` — `R2_BUCKET` split on `+`, whitespace stripped, each validated against `^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`; an invalid name raises `ValueError` at startup naming it.
- Produces: `r2.list_keys()` yields objects whose `.key` is `<bucket>/<object-key>` (D8); `r2.get_object(file_key)` and `r2.get_stream(file_key)` split the first segment off as the bucket and REFUSE a bucket not in `buckets()`.

- [ ] **Step 1: Failing tests** covering: `R2_BUCKET=a+b` lists both mirrors' objects, each prefixed with its bucket; the same object key in two buckets yields two distinct `files` rows; `get_object("c/x")` with `c` not configured raises; `R2_BUCKET=Bad_Name` raises `ValueError`; a single-bucket deploy stores `claude/<key>`; the orphan sweep removes a file whose object vanished from bucket `a` and leaves bucket `b`'s same-named key untouched.
- [ ] **Step 2:** implement. The file-mode mirror resolves `<root>/<bucket>/<key>` as it does today; the S3 path lists and fetches per bucket. Keep `_safe_join`'s traversal check on the object-key part.
- [ ] **Step 3:** `classify` strips the leading bucket segment before applying Task 3's rules.
- [ ] **Step 4:** deploying this re-keys every stored file: the next ingest sweeps the old unprefixed rows as orphans and reinserts every object under its prefixed key. Say so in the commit message. No `PARSER_VERSION` bump is needed — the new keys force the reparse themselves.
- [ ] **Step 5:** gates, commit `Ingest several buckets per deploy, file identity qualified by bucket`.

---

### Task 5: Branding from config

**Files:**
- Modify: `backend/app.py` (FastAPI title, `index.html` rewrite), `public/index.html`, `src/app.jsx` (logo, PNG filename)
- Create: `tests/test_branding.py`

**Interfaces:**
- Produces: `window.BRAND = {name, title, description}` injected beside `window.BACKEND_URL`.

- [ ] **Step 1: Failing tests:** with `APP_NAME=codexmeter`, `APP_TITLE=Codexmeter <x>` set, the served `/` contains `<title>Codexmeter &lt;x&gt;</title>` (HTML-escaped) and `window.BRAND` as JSON with `</` escaped; with nothing set, the served page is byte-identical to today's except for the injected `window.BRAND`; `src/app.jsx` contains no literal `CLAUDIT` or `claudit_` (source-level check in the style of `tests/test_panel_wiring.py`).
- [ ] **Step 2:** implement; defaults are today's strings (`claudit`, `claudit · Claude Code Usage Dashboard`, today's meta description).
- [ ] **Step 3:** gates, commit `Take the app's name, title and description from config`.

---

### Task 6: One browser parser for every format

**Files:**
- Create: `src/parser-lanes.js` (codexmeter `src/parser.js`'s Codex and Kimi parsing, minus its rate table)
- Modify: `src/parser.js` (`parseTranscript` sniffs the first JSON line and delegates), `public/index.html` (load `parser-lanes.js` before `parser.js`), `src/dashboard-charts-extra.jsx` (`capForModel`: 272,000 for `gpt-*`, 256,000 for `kimi-*`)
- Create: `tests/test_parser_js_lanes.py`

- [ ] **Step 1: Failing tests,** run through node as `tests/test_parser_js_mirror.py` does: each Task 2 fixture parsed in the browser yields the same per-record token totals and cost as `backend.parse.parse_file`; `capForModel('gpt-6-sol') == 272000`, `capForModel('kimi-k3') == 256000`, `capForModel('claude-opus-5')` unchanged.
- [ ] **Step 2:** port and wire; one rate table only, the one in `src/parser.js` (Task 1 already mirrored the lane rows).
- [ ] **Step 3:** gates, commit `Parse Codex and Kimi transcripts in the browser too`.

---

### Task 7: Cutover (lead-run, not dispatched)

- [ ] Final whole-branch review; open a PR from `merge-lane-meters` to `master` with the repo's PR template; merge after approval and green CI.
- [ ] For each of codexmeter and kimimeter: clone claudit into `/root/codexmeter-app` / `/root/kimimeter-app`, create a fresh database (`codex` / `kimi`), write `.env` with `R2_BUCKET`, `DATABASE_URL_VIZ`, the shared auth DB, `APP_NAME` / `APP_TITLE`, then repoint the unit's `WorkingDirectory` and `EnvironmentFile`. Keep the old checkouts and the old `codexmeter` / `kimimeter` databases untouched until verification passes: that is the rollback.
- [ ] Verify each new deploy against its old database: per-model requests, token sums and cost within 0.5%, with any difference explained (the 1h undeclared-TTL rule and GPT-6 relabelling are expected sources). Check `/health`, the dashboard over the live hostname, and one transcript view.
- [ ] Then retire the old checkouts and archive the two repos.

---

## Self-review

- **Coverage:** D1-D3 → Task 2; D4-D6 → Task 1; D7 → Task 2 step 1; D8 → Task 4; D9 → Task 5; D10 → Task 7 (`.env` keeps `CLAUDIT_*`). The browser half of D1 → Task 6.
- **Names used across tasks:** `compute_cost(..., long_context=)` (T1 → T2), `sniff_format` / `to_claudit` (T2), `classify` / `KeyInfo` / `project_marker` (T3 → T4), `buckets()` (T4). Consistent.
- **Known risk:** codexmeter's ~400 tests encode Codex specifics (cumulative token differencing, compaction replay). Task 2 step 5 treats any non-naming failure as a regression to report, not to paper over.
