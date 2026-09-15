"""What happened between one API request and the next, attached to the next.

Claude Code re-sends the whole conversation on every request, so anything
that changes the request prefix — a blocking Stop hook, an interrupt, a
model or effort switch, a tool-list change, a CLI upgrade on resume — costs
a full prompt-cache rewrite on the NEXT request. The transcript records
those events as lines between two requests. This tracker folds them into a
small flag set carried on the next request's record (``records.turn_flags``),
so the cause of a cache miss is a GROUP BY, not a re-read of the raw file.

Three shapes do not fit the fold-forward rule and get their own handling:

- A ``prompt_snapshot`` attachment describes the request BEFORE it: the
  harness writes it after that response, holding the tool list and system
  prompt as sent. Its flags (``tools_change``, ``system_change``,
  ``prompt_rerender``) are therefore BACKFILLED onto the last request's
  flag list, not folded onto the next one. Only digests of the snapshot
  are kept — the tool block alone is ~265 KB.
- A ``session_context`` attachment marks a process start on this file.
  It lands on the next request like any window event, as ``resume``; on
  the first request of a file it is simply the launch.
- The record ``cwd`` follows the shell cwd tool by tool, but the system
  prompt only re-resolves at a turn boundary. ``cwd_switch`` says the cwd
  differs from the previous request; ``cwd_rebuild`` says a turn OPENED
  on a cwd different from the previous turn start, which is the change
  that re-renders the prefix.

Flags are harness-generic names for transcript shapes, not for a
particular fix; a check that wants "the bug fixed in 2.1.259" maps a flag
to a version at read time.
"""
from __future__ import annotations

import hashlib

from orjson import OPT_SORT_KEYS, dumps

from backend.constants import INTERRUPT_MARKER

FLAG_STOP_HOOK_BLOCK = "stop_hook_block"   # a Stop hook refused the stop
FLAG_INTERRUPT = "interrupt"               # the user cut the reply off
FLAG_COMPACT = "compact"                   # context was compacted (new prefix, expected)
FLAG_API_ERROR = "api_error"               # a request failed and was retried
FLAG_MODEL_SWITCH = "model_switch"         # /model
FLAG_EFFORT_SWITCH = "effort_switch"       # /effort, or the effort field changed
FLAG_ADVISOR_SWITCH = "advisor_switch"     # advisorModel appeared, vanished or changed
FLAG_VERSION_SWITCH = "version_switch"     # the CLI version changed mid-file (resume on a new build)
FLAG_TOOLS_DELTA = "tools_delta"           # the deferred tool list changed
FLAG_IMAGE_RESULT = "image_result"         # a tool result carried an image
FLAG_SLASH_COMMAND = "slash_command"       # any other local slash command
FLAG_USER_PROMPT = "user_prompt"           # a substantive user message: a new turn began
FLAG_DATE_CHANGE = "date_change"           # the harness noted a calendar-date rollover
FLAG_USER_REJECTED = "user_rejected"       # the user declined a tool call
FLAG_CWD_SWITCH = "cwd_switch"             # the working directory changed
FLAG_AWAY_SUMMARY = "away_summary"         # a /recap-style away summary was injected
FLAG_TOOLS_CHANGE = "tools_change"         # prompt_snapshot: the tool block differs from the previous snapshot
FLAG_SYSTEM_CHANGE = "system_change"       # prompt_snapshot: the system prompt differs from the previous snapshot
FLAG_PROMPT_RERENDER = "prompt_rerender"   # prompt_snapshot written, tools and system prompt unchanged
FLAG_RESUME = "resume"                     # a process started on this file (session_context): a resume unless first request
FLAG_CWD_REBUILD = "cwd_rebuild"           # turn opened with a cwd different from the previous turn start


def _digest(value: object) -> bytes:
    return hashlib.blake2b(dumps(value, option=OPT_SORT_KEYS), digest_size=16).digest()


class TurnWindow:
    """Feed every parsed line to ``observe``; call ``take`` on the first
    line of each new request to collect the window that preceded it.

    ``take`` keeps a reference to the flag list it returned, and a later
    ``prompt_snapshot`` amends that list IN PLACE. The parser stores the
    returned list directly in the record, which is what makes the
    backfill land on the previous request without a parser-side hook.
    Before any request has been taken, snapshot flags join ``flags`` and
    reach the first request instead."""

    def __init__(self) -> None:
        self.flags: set[str] = set()
        self.tool_results = 0
        self._prev: dict | None = None
        self._last_flags: list[str] | None = None            # the list take() last returned
        self._snapshot: tuple[bytes, bytes] | None = None   # (tools, system) digests
        self._turn_cwd: str | None = None                   # cwd at the last turn-opening request

    def observe(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "system":
            self._observe_system(obj)
        elif kind == "user":
            self._observe_user(obj)
        elif kind == "attachment":
            att = obj.get("attachment")
            if not isinstance(att, dict):
                return
            att_type = att.get("type")
            if att_type == "deferred_tools_delta":
                self.flags.add(FLAG_TOOLS_DELTA)
            elif att_type == "date_change":
                self.flags.add(FLAG_DATE_CHANGE)
            elif att_type == "session_context":
                self.flags.add(FLAG_RESUME)
            elif att_type == "prompt_snapshot":
                self._observe_snapshot(att)

    def _observe_snapshot(self, att: dict) -> None:
        """Diff the snapshot against the previous one with tools; the
        preamble snapshot carries no tool list and describes no request.
        ``cliPrefix`` is ignored by design: ``version_switch`` already
        covers the CLI build. Two snapshots between the same pair of
        requests accumulate onto one record, and a snapshot for a request
        that produced no usage line lands on the previous successful
        request — rare shapes, documented rather than coded around."""
        tools = att.get("tools")
        if not isinstance(tools, list) or not tools:
            return
        digests = (_digest(tools), _digest(att.get("systemPrompt")))
        prev, self._snapshot = self._snapshot, digests
        if prev is None:
            return
        new_flags = set()
        if digests[0] != prev[0]:
            new_flags.add(FLAG_TOOLS_CHANGE)
        if digests[1] != prev[1]:
            new_flags.add(FLAG_SYSTEM_CHANGE)
        if digests == prev:
            new_flags.add(FLAG_PROMPT_RERENDER)
        self._backfill(new_flags)

    def _backfill(self, new_flags: set[str]) -> None:
        """Amend the flags of the request BEFORE the line just observed."""
        if self._last_flags is None:
            self.flags |= new_flags
        else:
            self._last_flags[:] = sorted(set(self._last_flags) | new_flags)

    def _observe_system(self, obj: dict) -> None:
        subtype = obj.get("subtype")
        if subtype == "stop_hook_summary" and obj.get("preventedContinuation"):
            self.flags.add(FLAG_STOP_HOOK_BLOCK)
        elif subtype == "compact_boundary":
            self.flags.add(FLAG_COMPACT)
        elif subtype == "api_error":
            self.flags.add(FLAG_API_ERROR)
        elif subtype == "away_summary":
            self.flags.add(FLAG_AWAY_SUMMARY)
        elif subtype == "local_command":
            content = str(obj.get("content") or "")
            if "<command-name>/model<" in content:
                self.flags.add(FLAG_MODEL_SWITCH)
            elif "<command-name>/effort<" in content:
                self.flags.add(FLAG_EFFORT_SWITCH)
            elif "<command-name>" in content:
                self.flags.add(FLAG_SLASH_COMMAND)

    def _observe_user(self, obj: dict) -> None:
        if obj.get("isCompactSummary"):
            self.flags.add(FLAG_COMPACT)
        if obj.get("interruptedMessageId"):
            self.flags.add(FLAG_INTERRUPT)
        if obj.get("toolDenialKind") == "user-rejected":
            self.flags.add(FLAG_USER_REJECTED)
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            self._observe_user_text(obj, content)
            return
        if not isinstance(content, list):
            return
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                self._observe_user_text(obj, str(blk.get("text") or ""))
            elif btype == "tool_result":
                self.tool_results += 1
                inner = blk.get("content")
                if isinstance(inner, list) and any(
                        isinstance(b, dict) and b.get("type") == "image" for b in inner):
                    self.flags.add(FLAG_IMAGE_RESULT)

    def _observe_user_text(self, obj: dict, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        if stripped.startswith(INTERRUPT_MARKER):
            self.flags.add(FLAG_INTERRUPT)
        elif obj.get("isMeta") and stripped.startswith("Stop hook feedback"):
            self.flags.add(FLAG_STOP_HOOK_BLOCK)
        elif not stripped.startswith("<") and not obj.get("isMeta"):
            # Typed, queued or task-notification prompts; instrumentation
            # (<bash-input>, <command-name>, <task-notification>...) is not
            # a new turn in the user's sense. Same test parse.handle_user_text
            # applies for prompt_count.
            self.flags.add(FLAG_USER_PROMPT)

    def take(self, obj: dict) -> tuple[list[str], int, str | None]:
        """Flags, tool-result count and CLI version for the request whose
        first line is ``obj``. Switches are detected against the previous
        request's line, so they need no transcript line of their own."""
        flags = set(self.flags)
        cur = {"version": obj.get("version"), "effort": obj.get("effort"),
               "advisor": obj.get("advisorModel"), "cwd": obj.get("cwd")}
        if self._prev is not None:
            for key, flag in (("version", FLAG_VERSION_SWITCH), ("effort", FLAG_EFFORT_SWITCH),
                              ("advisor", FLAG_ADVISOR_SWITCH), ("cwd", FLAG_CWD_SWITCH)):
                if cur[key] != self._prev[key]:
                    flags.add(flag)
        self._prev = cur
        if self.tool_results == 0:      # turn-opening: the prefix re-resolves from this cwd
            if self._turn_cwd is not None and cur["cwd"] != self._turn_cwd:
                flags.add(FLAG_CWD_REBUILD)
            self._turn_cwd = cur["cwd"]
        n = self.tool_results
        self.flags.clear()
        self.tool_results = 0
        version = cur["version"]
        self._last_flags = sorted(flags)
        return self._last_flags, n, (str(version) if version else None)
