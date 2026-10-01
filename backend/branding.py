"""The deploy's user-visible branding: APP_NAME, APP_TITLE,
APP_DESCRIPTION, APP_PRIVACY_NOTICE_URL.

One codebase is deployed over several buckets (claudit, codexmeter,
kimimeter); which name the UI shows comes from the environment, never
from the code. Defaults reproduce today's claudit strings exactly, so a
deploy that sets none of them is byte-identical to before.

Three escaping contexts, deliberately different (SV-BRAND-ESCAPE):
- HTML text/attribute contexts — the page <title>, the meta description,
  the login heading — take ``html.escape``-ed values.
- The window.BRAND payload lives inside a <script> element, where
  html-escaping would CORRUPT the JS string (`&amp;` stays `&amp;`).
  The context-correct guard is JS-level: ``script_json`` rewrites `</`
  to `<\\/` so the payload can never close the tag it lives in, and
  escapes the three further sequences that are unsafe inside a script
  or its JSON string literal: `<!--` (an HTML comment open, which
  starts script-hiding content in legacy parsing), and the raw
  U+2028/U+2029 line separators, which are line terminators to a JS
  string literal despite being ordinary whitespace to JSON.
- A URL attribute — the sign-in page's privacy-notice ``href`` — is an
  allow-list, not an escape: html-escaping keeps ``javascript:``
  runnable, because the browser entity-decodes and strips whitespace
  BEFORE it parses the scheme. ``url_attr`` accepts only http/https
  URLs and site-relative paths, returns the value already
  html-escaped for a double-quoted attribute, and refuses anything
  else: the link is dropped and a warning logged.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re

log = logging.getLogger("claudit.branding")

DEFAULT_NAME = "claudit"
DEFAULT_TITLE = "claudit · Claude Code Usage Dashboard"
DEFAULT_DESCRIPTION = (
    "Self-hosted dashboards for Claude Code session JSONL: cost-by-model, "
    "token breakdown, prompt-cache TTL split, response sizes, per-session "
    "context growth, session burn rate, tool-usage ratio, reply latency."
)


def _branded(env: str, default: str) -> str:
    """The env value when set to something visible, else the default.

    An EMPTY or whitespace-only setting would render an empty logo and
    export a leading-underscore filename, so it reads as unset.
    """
    return os.environ.get(env, "").strip() or default


def brand_name() -> str:
    return _branded("APP_NAME", DEFAULT_NAME)


def brand_title() -> str:
    return _branded("APP_TITLE", DEFAULT_TITLE)


def brand_description() -> str:
    return _branded("APP_DESCRIPTION", DEFAULT_DESCRIPTION)


def privacy_notice_url() -> str:
    return _branded("APP_PRIVACY_NOTICE_URL", "")


_C0_SPACE = "".join(map(chr, range(0x21)))  # C0 controls + space
_URL_ATTR_RE = re.compile(r"(?:https?://|/(?!/))")


def url_attr(url: str) -> str | None:
    """The value for a URL-bearing HTML attribute — the sign-in page's
    privacy-notice ``href`` (issue #365) — or None when refused.

    An allow-list, never a ``javascript:`` deny-list: html-escaping
    alone cannot make an href safe, because the browser decodes
    entities and strips whitespace BEFORE it parses the scheme. After
    stripping the leading/trailing C0-controls-and-space the URL parser
    strips, the value must be an ``http://``/``https://`` URL or a
    site-relative path (leading ``/``, not ``//`` — that is
    scheme-relative). A tab or newline INSIDE the value is removed by
    the URL parser but not here, so ``jav\\tascript:`` is refused too:
    over-refusing a config value is safe and visible, under-refusing
    is XSS.

    A RAW BACKSLASH is refused wherever it appears (#438). The URL
    parser maps ``\\`` to ``/`` for a special scheme — http(s) here —
    so ``/\\host`` is the ``//host`` scheme-relative escape spelled the
    other way, and ``https://\\host`` names a different host than the
    value reads as; a tab or newline between the two is stripped by the
    parser and turns ``/\\t\\host`` into the same escape. Refusing the
    character outright closes the shape wherever the parser would read
    it as a ``/``, on either branch.

    The refusal reaches past the path: the parser rewrites a raw ``\\``
    to ``/`` in the AUTHORITY and the PATH, but leaves it in the QUERY
    and the FRAGMENT (``/p?a=b\\c`` keeps its backslash, node-verified),
    so a query or fragment naming a literal backslash is refused too.
    That is over-refusing two exotic values in the direction this
    function's own rule calls safe and visible — the dropped link and
    the logged warning — and ``%5C`` is the spelling that survives.
    Why the shape is refused at all is measured, not argued: a URL that
    means itself is written ``%5C``, which no parser rewrites.

    A refused value drops the link — a display-only setting must not
    take the service down at startup — and logs a warning, so the
    operator sees the misconfiguration (SV-BRAND-ESCAPE).
    """
    value = url.strip(_C0_SPACE)
    if not value:
        return None
    if not _URL_ATTR_RE.match(value) or "\\" in value:
        log.warning(
            "refusing %r as a URL attribute (SV-BRAND-ESCAPE): only "
            "http:// and https:// URLs and site-relative paths "
            "(leading /, no backslash — a browser reads it as /) are "
            "allowed; dropping the link",
            value,
        )
        return None
    return html.escape(value, quote=True)


def brand() -> dict[str, str]:
    """The three values as the window.BRAND payload dict."""
    return {
        "name": brand_name(),
        "title": brand_title(),
        "description": brand_description(),
    }


def script_json(value: object) -> str:
    """JSON safe inside a <script> block: none of the sequences that can
    close the tag, open a script-hiding HTML comment, or terminate a JS
    string literal mid-payload can appear.
    """
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
        .replace("<!--", "<\\!--")
    )


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
