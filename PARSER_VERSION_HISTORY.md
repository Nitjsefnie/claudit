# PARSER_VERSION history

`PARSER_VERSION` lives in `backend/parser_version.py` — its own module
since issue #986, disjoint from the file the hourly pricing bot
rewrites — and the history of every bump is kept here. That module is
kept dependency-free, like `constants.py`: any module may import it,
and it imports nothing. It lives apart from every other version
constant on purpose: the hourly pricing bot rewrites
`backend/constants.py` to bump `PRICING_VERSION`, and a PARSER_VERSION
sharing that file textually conflicted with every bot commit, so each
parser-version change rebased on the bot's commits first. The bot's
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
- **111** — issue #1023 task 1: Bash write targets now include link names,
  touch operands, local rsync destinations, explicit extraction directories,
  curl/wget output files and Python venv directories. Reparse stored calls
  to populate their previously empty write-target arrays. Downloads,
  extraction, rsync and venv creation use the per-call unknown-size churn
  fallback; touch and ln add zero content lines.
- **112** — issue #1023 task 2: Bash write targets and unknown-size churn
  include worktree add, explicit checkout pathspecs, worktree restore,
  compiler outputs, pdftotext outputs, initdb data directories, explicit
  clone directories, git init, Perl in-place edits and rustfmt files.
  Bare clones no longer derive a directory from the URL. Reparse stored
  calls to populate their literal targets and per-call churn estimates.
