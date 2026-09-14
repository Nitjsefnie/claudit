"""What happened between one API request and the next, attached to the next.

Claude Code re-sends the whole conversation on every request, so anything
that changes the request prefix — a blocking Stop hook, an interrupt, a
model or effort switch, a tool-list change, a CLI upgrade on resume — costs
a full prompt-cache rewrite on the NEXT request. The transcript records
those events as lines between two requests. This tracker folds them into a
small flag set carried on the next request's record (``records.turn_flags``),
so the cause of a cache miss is a GROUP BY, not a re-read of the raw file.

Flags are harness-generic names for transcript shapes, not for a
particular fix; a check that wants "the bug fixed in 2.1.259" maps a flag
to a version at read time.
"""
from __future__ import annotations

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


class TurnWindow:
    """Feed every parsed line to ``observe``; call ``take`` on the first
    line of each new request to collect the window that preceded it."""

    def __init__(self) -> None:
        self.flags: set[str] = set()
        self.tool_results = 0
        self._prev: dict | None = None

    def observe(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "system":
            self._observe_system(obj)
        elif kind == "user":
            self._observe_user(obj)
        elif kind == "attachment":
            att = obj.get("attachment")
            att_type = att.get("type") if isinstance(att, dict) else None
            if att_type == "deferred_tools_delta":
                self.flags.add(FLAG_TOOLS_DELTA)
            elif att_type == "date_change":
                self.flags.add(FLAG_DATE_CHANGE)

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
        n = self.tool_results
        self.flags.clear()
        self.tool_results = 0
        version = cur["version"]
        return sorted(flags), n, (str(version) if version else None)
