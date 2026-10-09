"""Estimate line churn from literal shell payloads inside Codex tool calls."""
from __future__ import annotations

import json
import re

from backend.bash_argv import argv_churn
from backend.bash_churn import bash_churn

_CODEX_CMD_STR = re.compile(r"""["']?cmd["']?\s*:\s*(?=")""")
_CODEX_CMD_ARGV = re.compile(r"""["']?command["']?\s*:\s*(?=\[)""")


def _codex_json_values(src: str, pattern: re.Pattern[str]):
    """Every JSON value in `src` introduced by `pattern`.

    Yields the decoded value and skips whatever span it occupied, so a
    key that appears again INSIDE a value already read is not mined a
    second time.
    """
    decoder = json.JSONDecoder()
    consumed_to = 0
    for match in pattern.finditer(src):
        if match.end() < consumed_to:
            continue
        try:
            value, end = decoder.raw_decode(src, match.end())
        except ValueError:
            continue
        consumed_to = end
        yield value


def _codex_program_churn(src: str) -> tuple[int, int]:
    """Churn enumerable from ONE exec program's shell payloads.

    A custom_tool_call's input is a JS program, so the shell text is a
    string literal inside it: `tools.exec_command({cmd:"..."})`, and
    `tools.monitor({command:["bash","-lc","..."]})` for the API that
    takes an argv array instead. Both values are written as JSON, so
    raw_decode reads them back exactly — escapes, embedded quotes and
    all — rather than a regex guessing where the literal ends.

    stdlib json, not orjson, because only JSONDecoder.raw_decode can
    start at an offset inside a larger document and report where the
    value ended — orjson parses whole documents only.

    Backtick templates (3% of cmd values) are skipped: JS interpolation
    means the text is not the command, and a template is not JSON.

    A decoded value's own span is skipped afterwards, so a script that
    happens to contain `cmd:"..."` in its own text is not mined twice.
    """
    added = deleted = 0
    for value in _codex_json_values(src, _CODEX_CMD_STR):
        a, d = bash_churn(str(value))
        added += a
        deleted += d
    for value in _codex_json_values(src, _CODEX_CMD_ARGV):
        a, d = argv_churn(value)
        added += a
        deleted += d
    return added, deleted
