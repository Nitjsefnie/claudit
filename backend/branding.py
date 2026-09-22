"""The deploy's user-visible branding: APP_NAME, APP_TITLE, APP_DESCRIPTION.

One codebase is deployed over several buckets (claudit, codexmeter,
kimimeter); which name the UI shows comes from the environment, never
from the code. Defaults reproduce today's claudit strings exactly, so a
deploy that sets none of the three is byte-identical to before.

Two escaping contexts, deliberately different (SV-BRAND-ESCAPE):
- HTML text/attribute contexts — the page <title>, the meta description,
  the login heading — take ``html.escape``-ed values.
- The window.BRAND payload lives inside a <script> element, where
  html-escaping would CORRUPT the JS string (`&amp;` stays `&amp;`).
  The context-correct guard is JS-level: ``script_json`` rewrites `</`
  to `<\\/` so the payload can never close the tag it lives in.
"""
from __future__ import annotations

import html
import json
import os
import re

DEFAULT_NAME = "claudit"
DEFAULT_TITLE = "claudit · Claude Code Usage Dashboard"
DEFAULT_DESCRIPTION = (
    "Self-hosted dashboards for Claude Code session JSONL: cost-by-model, "
    "token breakdown, prompt-cache TTL split, response sizes, per-session "
    "context growth, session burn rate, tool-usage ratio, reply latency."
)


def brand_name() -> str:
    return os.environ.get("APP_NAME", DEFAULT_NAME)


def brand_title() -> str:
    return os.environ.get("APP_TITLE", DEFAULT_TITLE)


def brand_description() -> str:
    return os.environ.get("APP_DESCRIPTION", DEFAULT_DESCRIPTION)


def brand() -> dict[str, str]:
    """The three values as the window.BRAND payload dict."""
    return {
        "name": brand_name(),
        "title": brand_title(),
        "description": brand_description(),
    }


def script_json(value: object) -> str:
    """JSON safe inside a <script> block: `</` cannot appear, so the
    payload cannot close (or open a closing context of) the script tag.
    """
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


_TITLE_RE = re.compile(r"<title>.*?</title>", re.S)
_META_DESC_RE = re.compile(r'(<meta\s+name="description"\s+content=").*?("\s*/>)')


def brand_page(page: str) -> str:
    """Rewrite a served index page's <title> and meta description to the
    deploy's branding. Unmatched (a template without those elements)
    leaves the page untouched — the shipped template carries both.
    """
    title = html.escape(brand_title(), quote=True)
    page = _TITLE_RE.sub(lambda _m: f"<title>{title}</title>", page, count=1)
    desc = html.escape(brand_description(), quote=True)
    return _META_DESC_RE.sub(
        lambda m: f"{m.group(1)}{desc}{m.group(2)}", page, count=1)
