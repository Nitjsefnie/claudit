---
name: Issue
about: The repository's filing form — one shape for bugs and enhancements alike
title: ''
labels: ''
assignees: ''
---

<!--
The Issue Filing Contract, rendered as a form.

  required     always present; when the answer is "none", say so explicitly
  conditional  always CHECKED, but dropped entirely — header included — when
               the condition is not met
  optional     dropped entirely when it does not apply

Section order is fixed. No new sections. The TITLE is load-bearing: one line,
plain language, no ticket-speak, no trailing punctuation — it gets pasted
verbatim into a PR's Bugs Discovered section, so it has to read standalone.
Write it last. Delete these comments before posting.
-->

## Description
<!-- required: observed behavior ONLY — what is actually wrong. Not the fix,
     not a hypothesis, not the mechanism, even a proved one. -->

## Expected Behavior
<!-- required: what should happen instead. -->

## Reproduction Steps
<!-- conditional: numbered steps that reliably trigger it. Not reliably
     reproducible? Drop this section and say so in Description instead of
     writing partial steps. -->

1.

## Environment / Context
<!-- optional: version, OS, config or data conditions — only when they matter
     to reproducing or understanding it. -->

## Discovered During
<!-- required: the PR, task or session that surfaced this — link or ID — even
     when the bug predates any PR. -->

## Suggested Fix
<!-- optional: a hypothesis or pointer, clearly marked unverified. Does not
     replace Description. -->
