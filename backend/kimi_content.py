"""Pure content and tool-call helpers for the kimi-code wire format."""
from __future__ import annotations

import json

from backend.json_shape import as_dict, dict_list


def _kc_parse_tool_call(tc: dict) -> tuple[str, str | None, str]:
    """Extract name, arguments, id from a kimi-code ToolCall (v1.0/v1.1)."""
    if tc.get("type") != "function":
        return "", None, ""
    tcid = tc.get("id", "")
    if "name" in tc:
        return str(tc.get("name", "")), tc.get("arguments"), tcid
    func = as_dict(tc.get("function") or {})
    return str(func.get("name", "")), func.get("arguments"), tcid


def _kc_args_to_input(args) -> dict:
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {"_raw": args}
    return {"_raw": args}


def _count_content_text(content: list[dict]) -> int:
    chars = 0
    for part in dict_list(content):
        if part.get("type") == "text":
            chars += len(str(part.get("text", "")))
    return chars
