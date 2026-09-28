"""Regression guards for Inspector transcript fetch failures (issue #252)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"


def _skip_quoted(src: str, start: int) -> int:
    """Return the index after a quoted JavaScript string."""
    quote = src[start]
    i = start + 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
        elif src[i] == quote:
            return i + 1
        else:
            i += 1
    raise AssertionError("unterminated quoted string in function source")


def _skip_comment(src: str, start: int) -> int:
    """Return the index after a JavaScript line or block comment."""
    if src.startswith("//", start):
        end = src.find("\n", start + 2)
        return len(src) if end < 0 else end
    if src.startswith("/*", start):
        end = src.find("*/", start + 2)
        if end < 0:
            raise AssertionError("unterminated comment in function source")
        return end + 2
    return start


def _matching_brace(src: str, start: int) -> int:
    """Find the matching closing brace outside strings and comments."""
    depth = 0
    i = start
    while i < len(src):
        char = src[i]
        if char in "'\"`":
            i = _skip_quoted(src, i)
            continue
        comment_end = _skip_comment(src, i)
        if comment_end != i:
            i = comment_end
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("could not find the matching function brace")


def _extract_function(src: str, name: str) -> str:
    """Extract one async function by balancing its JavaScript braces."""
    match = re.search(
        rf"async function {re.escape(name)}\s*\([^)]*\)\s*\{{", src
    )
    assert match, f"could not locate `async function {name}` in app.jsx"
    brace_start = src.index("{", match.start(), match.end())
    end = _matching_brace(src, brace_start)
    return src[match.start():end]


def test_fetch_transcript_text_reports_http_errors_and_returns_success_body():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    src = APP.read_text(encoding="utf-8")
    helper = _extract_function(src, "fetchTranscriptText")
    script = f"""
{helper}
const assert = require('assert');
(async () => {{
  await assert.rejects(
    fetchTranscriptText('missing', async () => new Response(
      '{{"detail":"session not found"}}', {{status: 404}})),
    err => err instanceof Error && err.message.includes('404') &&
      err.message.includes('session not found'));
  await assert.rejects(
    fetchTranscriptText('broken', async () => new Response('internal', {{status: 500}})),
    err => err instanceof Error && err.message.includes('500'));
  await assert.rejects(
    fetchTranscriptText('offline', async () => {{ throw new Error('network down'); }}),
    /network down/);
  const expected = '{{"type":"user"}}\\n';
  const actual = await fetchTranscriptText(
    'ok', async () => new Response(expected, {{status: 200}}));
  assert.strictEqual(actual, expected);
}})().catch(err => {{
  console.error(err);
  process.exitCode = 1;
}});
"""
    proc = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_load_from_backend_uses_checked_transcript_fetch():
    src = APP.read_text(encoding="utf-8")
    load = _extract_function(src, "loadFromBackend")
    assert re.search(r"fetchTranscriptText\s*\(\s*sessionId\s*\)", load), (
        "loadFromBackend must pass the response text from the checked helper "
        "to parseTranscript"
    )
    assert "r.text()" not in load, (
        "loadFromBackend must not parse text from an unchecked response"
    )


def test_session_route_shows_transcript_fetch_error_in_place_of_session_view():
    src = APP.read_text(encoding="utf-8")
    match = re.search(r"\{route === 'session' && \((.*?)\)\}", src, re.S)
    assert match, "could not locate the Inspector route render branch"
    branch = match.group(1)
    assert 'className="err"' in branch
    assert "transcript fetch failed" in branch
    assert "transcriptError" in branch
    assert "SessionView" in branch
