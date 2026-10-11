# claudit Doctrine

Local rules for the claudit repo.

## Domain rules — where the rest of the doctrine lives

This file holds the cross-cutting rules only. The domain rules are sibling
files in this directory, each carrying `paths` frontmatter so it loads into
context when you work on a file in its area:

- `claudit-doctrine-parser.md` — the parse spec and the parser-side rules
  (`SV-PARSER-SPEC`, `SV-BASH-CHURN`, `SV-CONTEXT-INTAKE`). Area:
  `backend/parse*.py`, `backend/codex_fork.py`, `backend/kimi_content.py`,
  `backend/bash_*.py`, `backend/turn_flags.py`, `backend/prompt_gate.py`,
  `src/parser*.js`, `src/record-dedup.js`, `src/ctx-input.js`,
  `fixtures/parser/`, `fixtures/codex/`.
- `claudit-doctrine-ingest.md` — schema, identity, dedup and rollups
  (`SV-SCHEMA-AUTOAPPLY`, `SV-SCHEMA-FAIL-FAST`, `SV-FILES-RECORDS`,
  `SV-CANONICAL-FLAG`, `SV-SUPPRESSED-MODELS`, `SV-PROJECT-ALIASES`,
  `SV-ROLLUP`, `SV-REKEY-CUTOVER`). Area: `backend/ingest*.py`,
  `backend/db.py`, `backend/schema*.sql`, `backend/key_layout.py`,
  `backend/lane_*.py`, `backend/project_aliases.py`,
  `backend/parse_common.py`, `backend/parse_lanes.py`,
  `backend/api_dashboard.py`, `backend/api_common.py`.
- `claudit-doctrine-pricing.md` — rate data, resolution and the refresh
  passes (`SV-DATED-RATES`, `SV-RATE-DATA`, `SV-RATE-REFRESH`,
  `SV-VENDOR-RATES`, `SV-REPRICE`, `SV-PROVIDER-RATES`, `SV-RATE-ESTIMATES`).
  Area: `src/pricing.json`, `src/pricing_notices.json`, `src/rates.js`,
  `backend/pricing*.py`, `backend/rate_*.py`, `backend/ingest_reprice.py`,
  `backend/long_context.py`, `scripts/ci/refresh_*.py`,
  `scripts/ci/backfill_provider_rates.py`,
  `scripts/ci/collapse_oscillating_rates.py`, `scripts/ci/price_band.py`.
- `claudit-doctrine-ci.md` — the CI ratchets and the test-data rule
  (`SV-CI-RATCHETS`, `SV-TEST-DATA`). Area: `.github/workflows/`,
  `.github/actions/`, `.github/ci-thresholds.json`, `.gitleaks.toml`,
  `scripts/ci/`, `tests/`.

A path-scoped rule loads when you open a file matching its area, but do not
rely on that alone: when you are planning or discussing an area without having
opened one of its files, read the domain file directly — it is a plain
Markdown file in this directory. Every `SV-*` rule lives in exactly one file.

## Cost accounting is split TTL, always (SV-COST-SPLIT)

Every cost computed from `usage` MUST split `cache_creation` into
`ephemeral_5m` (1.25× base input) and `ephemeral_1h` (2× base input).
Tokens with no `ephemeral_*` split are charged at the **1h rate**.
The web-search component is additive: `web_search_requests` times the
record's dated `web_search` rate. It never changes the token TTL split;
the read-time fold exposes it in a separate `web_search` cost bucket.
Every site that prices or decomposes cost follows this —
`pricing.compute_cost`, the `/api/cache` fold
(`api_common._accumulate_buckets`), the browser parser in `src/parser.js`,
the dashboard projection in `src/app.jsx`, and the Inspector breakdown in
`src/token-breakdown.js`
— or a breakdown stops summing to its stored total.

Single-rate `cache_create` cost is BANNED. A rate change that reprices
stored records bumps `PRICING_VERSION` in `backend/constants.py`; the
next ingest reprices in place — no reparse, no R2 fetch (SV-REPRICE).

## Backend is the ONLY load path (SV-NO-LOCAL-UPLOAD)

No browser ingress for transcripts — no drag-drop, `FileReader`, zip
expansion or JSZip — no upload endpoint, and no server-side parsing of
operator-supplied jsonls. The only jsonls read come from R2, owned by
the same operator. Reintroduce none of them, file picker included.

`src/parser.js` is NOT dead code: `loadFromBackend()` parses
`/api/sessions/{id}/transcript` client-side via `parseTranscript` +
`computeSessionStats`, and the Token Breakdown panel prices rows via
`window.rateForModel`. It reads rates from `src/pricing.json`
(SV-RATE-DATA) and resolves them per SV-PARSER-SPEC.

## Bundle distribution NOT applicable (SV-NO-BUNDLE)

claudit does not ship via `claude-setup.zip`; it ships via git +
`pip install -r backend/requirements.txt`.

## Test fixtures stay small (SV-FIXTURE-SIZE)

- `fixtures/parser/*.jsonl`: hand-crafted single-record samples, each
  under 1 KB.
- `fixtures/codex/`: EXEMPT from the cap, but grep it clean of real
  paths, ids and secrets before committing.
- `fixtures/r2_mini/`: the end-to-end mini mirror (2 projects, 4
  sessions, 1 sidecar, 1 cross-session shared uuid).

Don't grow them. Larger samples stay in a local mirror outside the
working tree, reached via `R2_ENDPOINT`.

## Develop against the full corpus, never the local tree (SV-FULL-CORPUS)

Measurements, probes and panel prototypes read the R2 corpus (or a full
mirror), NEVER Claude Code's live session tree. That tree is pruned, so
a rate derived from it is silently biased. Use `backend/r2.py`
(`list_keys` / `get_stream`) so a probe honours `R2_ENDPOINT`. Stratify
and state the sample size; never present a corpus-wide claim from an
unstated subset.

## No external parser, vendored or invoked (SV-READ-ONLY-CANONICAL)

Parsing is self-contained: the backend never invokes, imports or shells
out to a parser or script outside this repository, and no copy, symlink
or hardlink of one is committed. Drift between the in-repo parsers is
fixed here (SV-PARSER-SPEC).

## A token type may be a SUBSET (SV-SUBSET-TOKENS)

`records.thinking_tokens` is part of `output_tokens`, not a sixth billed
slice: the API reports it under `usage.output_tokens_details`, and
`pricing` never sees it. It is in `api_dashboard.TOKEN_TYPE_FIELDS`
because that tuple drives zero-suppression and panel order. It must
NEVER enter an arithmetic total: not `t.total` in `src/app.jsx`, not the
Token Breakdown rows, not a cost. `usage_rollup` carries it as a
pre-aggregate — summed, never added to the other types.

## Failure causes and dispatches are stored, not re-derived (SV-WHY-COLUMNS)

`tool_uses.is_error` says THAT a call failed. Four columns say why, and
what a dispatch asked for:

- `error_kind` — `rejected` | `tool_error` | `failed`, NULL unless the
  call errored. HARNESS-GENERIC: a PreToolUse hook denial carries the
  deploy's wording, so it lands in `failed`; never add a kind for one
  operator's hooks. `tool_error` means the call RAN (a
  `<tool_use_error>` wrapper, or a Bash result opening `Exit code N`).
  `rejected`/`failed` never ran, so they carry no `write_targets` and no
  churn. Lane wires have no status field, so the lane classifier reads
  the failure text: errored and not a recognizable rejection →
  `tool_error`; errored with no readable text → `failed`.
- `error_text` — the first `parse.ERROR_TEXT_MAX` chars of the failed
  result. Grouping on it separates hook denials from real failures, so
  the parser needs no hook vocabulary.
- `agent_type` / `agent_model` — from `Agent`/`Task` call arguments, a
  Codex `spawn_agent` / `multi_agent_v1__spawn_agent` call's arguments,
  and a Kimi `Agent` call's (`subagent_type`, `model`).
  `files.agent_type` records what RAN (only when the subagent wrote a
  JSONL); these record what was ASKED, so a fileless dispatch is still
  attributable. Codex dispatches keep `dispatch_prompt_chars` /
  `dispatch_brief_ref` NULL: their `message` is encrypted.
  Both sides store the canonical name (`agent_types.canonical_agent_type`,
  issue #650): one role is one name across lanes (Claude's `Explore`,
  Kimi's `explore`, Codex's `explorer` are one), a plugin-namespaced
  type folds to the type (`superpowers:code-reviewer` → `code-reviewer`),
  Kimi's `coder` profile is the implementer role, and Codex's `worker` —
  the lone lane default — is not a role at all: it lands in
  `DEFAULT_AGENT_TYPE`, the bucket a transcript that named nothing shares. A workflow-tree
  sidecar's `agentType: workflow-subagent` is a container marker, not a
  role: it stands only when nothing more specific was recorded, and an
  in-band role outranks it (the standing sidecar precedence).

`tool_error_rollup` and `dispatch_rollup` carry the composable counts.
`error_text` never enters a rollup grain (unbounded cardinality).

## The parser and pricing versions are code, never the environment (SV-PARSER-VERSION)

`constants.PARSER_VERSION` invalidates stored PARSE results: bumping it
reparses every stored file, refetching each from R2.
`constants.PRICING_VERSION` invalidates stored PRICES: bumping it
reprices stale records in place from their stored tokens via
`pricing.compute_cost`, batched, no R2 fetch (SV-REPRICE). A
parse-semantics change bumps PARSER_VERSION; a rate-data change bumps
PRICING_VERSION. Both live in code so the bump ships in the same commit
as its cause; never reintroduce an env override.

## Brand values escape per context (SV-BRAND-ESCAPE)

`APP_NAME` / `APP_TITLE` / `APP_DESCRIPTION` / `APP_PRIVACY_NOTICE_URL`
are config, and config is hostile input. Each injection context has its
own function (html and script payload and the URL-attribute allow-list
in `backend/branding.py`, the export-filename slug in
`backend/api_export.py`); never hand-concatenate a brand value into a
page:

- HTML text/attributes (title, meta, logo, sign-in page): html-escape.
  `<title>` and `<meta>` are replaced before the injected script block
  in `public/index.html`, so the `count=1` substitutions hit the real
  elements.
- The `window.BRAND` script payload: script-context escaping, which
  also neutralises `</script>`, `<!--` and U+2028/U+2029 (raw ones are
  a JS string syntax error; `<!--` opens a legacy HTML-like comment).
- A URL attribute (the sign-in page's privacy-notice `href`):
  `branding.url_attr` — an ALLOW-LIST, never a `javascript:`
  deny-list: after stripping the leading/trailing C0-controls-and-space
  the URL parser strips, only `http://`/`https://` or a site-relative
  path (leading `/`, not `//`) is accepted, returned already
  html-escaped for the double-quoted attribute. A refused value drops
  the link and logs a warning (a display-only setting must not fail
  startup or drop silently). Escaping alone cannot make a URL safe: the
  browser entity-decodes and strips whitespace before it reads the
  scheme.
- The export-PNG `Content-Disposition` filename: slugified to
  `[A-Za-z0-9._-]`.

A new surface echoing a brand value MUST route through one of these —
not a copy, not a new escape, never `f"{value}"`.

