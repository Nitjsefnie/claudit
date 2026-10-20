# PARSER_VERSION history

`PARSER_VERSION` lives in `backend/parser_version.py`, outside the hourly
pricing bot's write surface, and the history of every bump is kept here.
That module is kept dependency-free, like `constants.py`: any module may import it,
and it imports nothing. It lives apart from every other version
constant on purpose: the hourly pricing bot rewrites
`backend/constants.py` to bump `PRICING_VERSION`. Both constants shared
that file, so every parser-version bump shared the bot's changed file
and rebased on the bot's hourly commits. The review's experiment found
that two isolated bumps merged without conflict markers. The bot's
write surface and that module are disjoint now, pinned by
`tests/test_parser_version_home.py`.

## What the constant is

The parse-output schema/semantics version. Ingest reparses every file
whose stored `parser_version` is older. This code version invalidates
parse output; `files.collection_flags` separately records the explicit
collection policy.

It lives in code, and not in the environment: a parser change and the
reparse it requires must travel in the same commit. When this was read
from `.env`, shipping a parser change without an operator also editing
that file left every stored row at the old semantics with nothing to
detect it, and a deploy that never set the variable at all sat forever
on the `"1"` default.

## Bump rule

BUMP `PARSER_VERSION` in the same commit as any change to `parse.py`
semantics or to the set of columns `parse_file()` emits. A rate change
in `src/pricing.json` reprices stored records instead of reparsing
them: bump `PRICING_VERSION` in `constants.py` for that.

## Per-version history

- **108** — issue #959: the persist path stores dim FK ids and file_id,
  and the startup migration drops `records`/`tool_uses`; without a bump
  the stored `parser_version` would match and ingest would skip the
  reparse that refills the dropped facts.
- **109** — issue #959 task 7: the migration drops and rebuilds the
  facts again (SMALLINT/INTEGER storage widths, widest-first order,
  `cli_version` as a dim, `read_kind` as boolean) and persist now
  stores empty read/write target arrays as NULL — same reason, same
  fix: the bump is what makes ingest refill what dropped.
- **110** — issue #959 task 8: `records.uuid` becomes the split column
  set (`uuid_col`/`seq`/`fallback`, `backend/uuid_split.py` — the split
  happens at persist, so `parse.py` output is unchanged), and the shape
  guard drops any records still carrying the TEXT uuid. Dropping rows
  alone cannot refill them: `files.parser_version` would still match,
  so the bump is what forces the reparse that restores the dropped
  facts.
- **111** — issue #1023: Bash write targets now include link names,
  touch operands, local rsync destinations, explicit extraction directories,
  curl/wget output files and Python venv directories. Reparse stored calls
  to populate their previously empty write-target arrays. Downloads,
  extraction, rsync and venv creation use the per-call unknown-size churn
  fallback; touch and ln add zero content lines.
- **112** — issue #1023: Bash write targets and unknown-size churn
  include worktree add, explicit checkout pathspecs, worktree restore,
  compiler outputs, pdftotext outputs, initdb data directories, clone
  directories, git init, Perl in-place edits and rustfmt files. Bare clones
  preserve the existing name derivation; initdb show-only modes write
  nothing. Reparse stored calls for their targets and per-call churn.
- **113** — issue #983: `backend/lane_generation.py` maps lane response
  windows to generation duration and output tokens per second. Codex
  anchors on user messages and tool outputs, advances on response lines,
  and binds the latest completed window only at advancing token counts
  in either journal ordering. Collection OFF stores NULL without creating
  a generation clock. Browser parsers do not mirror request timing.
- **114** — issue #983: both Kimi formats map request generation windows
  to `generation_s` and `output_tokens_per_s`. kimi-code `usage.record`
  follows `step.end` and books the completed cycle; legacy `StatusUpdate`
  with nonempty `token_usage` books the window whose lines precede it.
  Kimi-code steers anchor at delivery, and every `llm.error` invalidates
  its request's span. Collection OFF leaves both fields NULL.
- **115** — issue #983: Codex `web_search_call` and `local_shell_call`
  response items advance generation timing through their own timestamps.
  Reparse stored records to include these model-issued tool calls in
  the last-response-line duration and output-token rate. Web-search
  billing and advancing-token-only timing binding are unchanged.
- **116** — issues #1091 and #1092: recover Bash write targets across brace
  expansions, enumerate bounded comma-list operands and keep the destination
  for unresolved source expansions. Book explicit and inferred `gh repo clone`
  directories, ignoring git passthrough arguments after `--`. Brace recovery
  preserves scalar assignment values and also scans quoted captures. Line
  churn keeps its existing tokenizer and estimates.
