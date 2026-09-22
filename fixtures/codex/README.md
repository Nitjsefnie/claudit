# Codex rollout fixtures

**Every file here is generated, not captured.** None of it is an excerpt
of anyone's session: the prompts, replies, reasoning summaries, commands,
paths, diffs and ids are all invented, and the timestamps are a round
sequence starting at `2026-06-14T12:00:00Z`. They describe one toy
project at `/workspace/toy-project` — a `greeter` module and its tests —
carried across all seven files so they read as one corpus.

What is *not* invented is the grammar. Record `type` values
(`session_meta`, `turn_context`, `event_msg`, `response_item`,
`world_state`, `compacted`, `inter_agent_communication_metadata`), the
`payload.type` discriminators — including the `item.type` second
discriminator the `item_completed` envelope adds — the field names, and
the arithmetic relating `total_token_usage` to `last_token_usage` are
reproduced as `~/.codex/sessions/**/rollout-*.jsonl` emits them. A fixture that stops
reproducing the format is worse than no fixture, so the numbers below are
chosen to keep the parser's four costly properties (see the module
docstring of `backend/parse_codex.py`) detectable rather than to look
plausible.

| fixture | lines | what it carries |
|---|---|---|
| `rollout_fork_prefix.jsonl` | 47 | a fork — `session_meta` whose `session_id` is the PARENT thread's and whose `id` is this file's own — opening on 40,000,000 inherited cumulative tokens; 20 `token_count` events of which 2 repeat the previous snapshot; ~98% cache-read rate; declares no model |
| `rollout_model_switch.jsonl` | 48 | a mid-session model switch, `gpt-5.6-sol` through line 28 and `gpt-5.6-terra` from line 29, with `token_count` records on both sides; five turns including one `turn_aborted`; no `session_meta`, so it stands in for a window cut from the middle of a file |
| `rollout_sole_model_prefix.jsonl` | 25 | five `token_count` records BEFORE the file's only model declaration (line 20) — the backfill case a fork's replayed history creates |
| `rollout_patch_linked.jsonl` | 29 | seven tool calls (five `exec_command`, two `apply_patch`), each with its output, and both `patch_apply_end` events that belong to the `apply_patch` calls |
| `rollout_shell_churn.jsonl` | 8 | three exec programs whose shell payloads are the churn: a `cat > ... <<'EOF'` heredoc in a `cmd` string, a `tools.monitor({command:["bash","-lc",...]})` argv array, and a plain pytest run that must count zero. No `patch_apply_end` anywhere, so it isolates text-derived churn from the journalled kind |
| `rollout_patch_subagent.jsonl` | 11 | a `patch_apply_end` with **no** `apply_patch` tool call anywhere in the file — a subagent's edit, which the parent rollout journals without the call that made it |
| `rollout_item_completed.jsonl` | 15 | the post-2026-08-18 spelling: two `item_completed` `FileChange` items — one linked to an `apply_patch` call by turn, one `status: "failed"` and one unlinked — carrying all three change types (`update` with a `unified_diff`, `add` and `delete` with `content`), plus two `AgentMessage` items in place of `agent_message`. No `patch_apply_end` and no `event_msg/agent_message`, because no real file mixes the spellings |

## Invariants an edit must preserve

`tests/test_parse_codex.py` reads its expected numbers off these files, so
it will tell you when one moves. It cannot tell you when a fixture stops
being able to catch a wrong parser, which is what these rules are for.

1. **`total_token_usage` is cumulative and monotonic**, and equals
   `input_tokens + output_tokens`. `cached_input_tokens` and
   `cache_write_input_tokens` are SUBSETS of `input_tokens`;
   `reasoning_output_tokens` is a subset of `output_tokens`.
2. **The fork's inherited head has to dwarf its real work.** Its first
   snapshot carries 40,000,000 tokens of a parent's usage against a
   100,500-token first request, and its final cumulative counter is 17x
   the work the file actually did. Shrink that gap and a parser that
   bills the snapshot instead of the delta starts passing.
3. **The two duplicate `token_count` events** (lines 35 and 44) must
   repeat the preceding cumulative snapshot exactly. Line 35 is the shape
   a compaction emits — same totals, `last_token_usage` zeroed.
4. **The cache-read rate has to stay high** (~98%). At a low rate,
   adding the cache instead of subtracting it is a small enough error to
   slip past the assertions.
5. **Model declarations must sit between the requests they govern**, and
   `token_count` payloads must never name a model of their own.
6. **The two spellings must stay unmixed.** `rollout_item_completed.jsonl`
   carries no `patch_apply_end` and no `event_msg/agent_message`, and the
   other six carry no `item_completed`. Adding one to the other side would
   assert a file shape Codex has never emitted, and would hide a parser
   that double-counts a patch it sees twice.
7. **Requests within a thread must land on distinct cumulative totals**,
   because `<session_id>:<total_tokens>` is the record uuid that dedups a
   replayed request across files.

## What they deliberately do NOT cover

- **`cache_write_input_tokens`.** Zero on all 30,249 `token_count`
  payloads measured on the reference corpus, so a fixture carrying one
  would misrepresent the format. `tests/test_parse_codex.py` constructs
  the case in memory instead.
- **The long-context meter.** The largest request here is 230,000 input
  tokens against a 272,000 threshold; the largest measured real request
  was 243,093, so nothing crosses it. The multiplier is asserted on the
  rate function.
- **A rate-limit hit.** `rate_limit_reached_type` and
  `spend_control_reached` ride every `token_count` payload and are null
  on every one of them, as they were on every payload measured.
- **An item type that carries no measurement.** `Reasoning`,
  `CommandExecution`, `UserMessage`, `SubAgentActivity`,
  `ContextCompaction`, `Extension`, `ImageView` and `CollabAgentToolCall`
  are all real `item_completed` items, and the parser reads none of them —
  `CommandExecution` in particular restates a `custom_tool_call` that is
  already booked, so a fixture carrying one would only be able to catch a
  double count that the tool-call assertions already catch.

## Placeholders

`encrypted_content` is a real field on `response_item/reasoning` and on
the items a `compacted` record replays, so it is present — but its value
is the literal filler `gAAAAAB` followed by a run of `A`s. It is not
ciphertext, nothing decrypts it, and the parser never reads the field.

Ids follow the format's shapes without being drawn from it: threads and
turns are UUID-shaped runs of zeros (`00000000-0000-4000-8000-…`), and
tool calls, messages and reasoning items carry a `_synth_` infix, and
an item's `exec-`-prefixed id is a zero-run UUID of the same shape.
