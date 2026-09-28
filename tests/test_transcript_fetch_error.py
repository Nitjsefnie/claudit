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
    """Extract a named function by balancing its JavaScript braces."""
    match = re.search(
        rf"(?:async\s+)?function {re.escape(name)}\s*\([^)]*\)\s*\{{", src
    )
    assert match, f"could not locate `function {name}` in app.jsx"
    brace_start = match.end() - 1
    end = _matching_brace(src, brace_start)
    return src[match.start():end]


def _extract_arrow_body(src: str, name: str) -> str:
    """Extract the block body of a named object-property arrow callback."""
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{{",
        src,
    )
    assert match, f"could not locate the `{name}` callback"
    brace_start = match.end() - 1
    return src[brace_start:_matching_brace(src, brace_start)]


def _run_node(script: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("node") is None:
        raise RuntimeError("node is not installed")
    return subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )


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
    err => err instanceof Error && err.message ===
      'HTTP 404: session not found');
  await assert.rejects(
    fetchTranscriptText('broken', async () => new Response('internal', {{status: 500}})),
    err => err instanceof Error && err.message === 'HTTP 500');
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
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_transcript_loader_starts_synchronously_and_parses_successful_text():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    src = APP.read_text(encoding="utf-8")
    loader = _extract_function(src, "makeTranscriptLoader")
    script = f"""
{loader}
const assert = require('assert');
(async () => {{
  const events = [];
  let resolveFetch;
  const load = makeTranscriptLoader({{
    fetchText: sessionId => {{
      events.push(['fetch', sessionId]);
      return new Promise(resolve => {{ resolveFetch = resolve; }});
    }},
    parse: text => {{ events.push(['parse', text]); return {{parsed: text.toUpperCase()}}; }},
    onStart: () => events.push(['start']),
    onSuccess: tx => events.push(['success', tx]),
    onError: (kind, err) => events.push(['error', kind, err]),
  }});

  const pending = load('chosen');
  assert.deepStrictEqual(events, [['start'], ['fetch', 'chosen']]);
  resolveFetch('fetched body');
  await pending;
  assert.deepStrictEqual(events, [
    ['start'], ['fetch', 'chosen'], ['parse', 'fetched body'],
    ['success', {{parsed: 'FETCHED BODY'}}],
  ]);
}})().catch(err => {{
  console.error(err);
  process.exitCode = 1;
}});
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_transcript_loader_reports_http_error_without_parsing():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    src = APP.read_text(encoding="utf-8")
    loader = _extract_function(src, "makeTranscriptLoader")
    fetch = _extract_function(src, "fetchTranscriptText")
    script = f"""
{fetch}
{loader}
const assert = require('assert');
(async () => {{
  const events = [];
  let parseCalls = 0;
  const load = makeTranscriptLoader({{
    fetchText: sessionId => fetchTranscriptText(sessionId, async (url, options) => {{
      assert.strictEqual(url, '/api/sessions/missing/transcript');
      assert.deepStrictEqual(options, {{credentials: 'same-origin'}});
      return new Response('{{"detail":"session not found"}}', {{status: 404}});
    }}),
    parse: text => {{ parseCalls++; return text; }},
    onStart: () => events.push(['start']),
    onSuccess: tx => events.push(['success', tx]),
    onError: (kind, err) => events.push(['error', kind, err]),
  }});

  await load('missing');
  assert.strictEqual(parseCalls, 0);
  assert.deepStrictEqual(events[0], ['start']);
  assert.strictEqual(events[1][0], 'error');
  assert.strictEqual(events[1][1], 'fetch');
  assert(events[1][2] instanceof Error);
  assert.strictEqual(events[1][2].message, 'HTTP 404: session not found');
}})().catch(err => {{
  console.error(err);
  process.exitCode = 1;
}});
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_transcript_loader_reports_parse_error_after_successful_fetch():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    src = APP.read_text(encoding="utf-8")
    loader = _extract_function(src, "makeTranscriptLoader")
    fetch = _extract_function(src, "fetchTranscriptText")
    script = f"""
{fetch}
{loader}
const assert = require('assert');
(async () => {{
  const parseError = new Error('malformed transcript');
  const events = [];
  let parseInput;
  const load = makeTranscriptLoader({{
    fetchText: sessionId => fetchTranscriptText(sessionId, async (url, options) => {{
      assert.strictEqual(url, '/api/sessions/bad/transcript');
      return new Response('bad transcript', {{status: 200}});
    }}),
    parse: text => {{ parseInput = text; throw parseError; }},
    onStart: () => events.push(['start']),
    onSuccess: tx => events.push(['success', tx]),
    onError: (kind, err) => events.push(['error', kind, err]),
  }});

  await load('bad');
  assert.deepStrictEqual(events[0], ['start']);
  assert.strictEqual(parseInput, 'bad transcript');
  assert.strictEqual(events[1][0], 'error');
  assert.strictEqual(events[1][1], 'parse');
  assert.strictEqual(events[1][2], parseError);
  assert(events[1][2] instanceof Error);
}})().catch(err => {{
  console.error(err);
  process.exitCode = 1;
}});
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_transcript_loader_ignores_superseded_success_and_failure():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    src = APP.read_text(encoding="utf-8")
    loader = _extract_function(src, "makeTranscriptLoader")
    script = f"""
{loader}
const assert = require('assert');
function deferred() {{
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {{ resolve = res; reject = rej; }});
  return {{promise, resolve, reject}};
}}
(async () => {{
  const pending = {{A: deferred(), B: deferred()}};
  const events = [];
  const load = makeTranscriptLoader({{
    fetchText: sessionId => pending[sessionId].promise,
    parse: text => ({{parsed: text}}),
    onStart: () => events.push('start'),
    onSuccess: tx => events.push(['success', tx.parsed]),
    onError: (kind, err) => events.push(['error', kind, err]),
  }});

  const first = load('A');
  const second = load('B');
  assert.deepStrictEqual(events, ['start', 'start']);
  pending.B.resolve('B body');
  await second;
  pending.A.reject(new Error('A failed late'));
  await first;
  assert.deepStrictEqual(events, ['start', 'start', ['success', 'B body']]);

  const successPending = {{A: deferred(), B: deferred()}};
  const successes = [];
  const loadSuccess = makeTranscriptLoader({{
    fetchText: sessionId => successPending[sessionId].promise,
    parse: text => ({{parsed: text}}),
    onStart: () => {{}},
    onSuccess: tx => successes.push(tx.parsed),
    onError: (kind, err) => assert.fail(err.message),
  }});
  const slowSuccess = loadSuccess('A');
  const fastSuccess = loadSuccess('B');
  successPending.B.resolve('B body');
  await fastSuccess;
  successPending.A.resolve('A body');
  await slowSuccess;
  assert.deepStrictEqual(successes, ['B body']);
}})().catch(err => {{
  console.error(err);
  process.exitCode = 1;
}});
"""
    proc = _run_node(script)
    assert proc.returncode == 0, proc.stderr


def test_load_from_backend_uses_loader_without_fetching_directly():
    src = APP.read_text(encoding="utf-8")
    load = _extract_function(src, "loadFromBackend")
    assert re.search(r"transcriptLoaderRef\s*\.\s*current\s*\(\s*sessionId\s*\)", load), (
        "loadFromBackend must start the shared transcript loader"
    )
    assert not re.search(r"\bfetch\s*\(", load), (
        "loadFromBackend must not make an unchecked transcript request"
    )
    assert ".text()" not in load, (
        "loadFromBackend must not parse a second unchecked response body"
    )
    assert re.search(r"fetchText\s*:\s*fetchTranscriptText", src), (
        "the shared loader must use the checked transcript fetch helper"
    )

    assert re.search(
        r"const\s+transcriptLoaderRef\s*=\s*useRef\s*\(\s*null\s*\)", src
    ), "the transcript loader must survive App renders"
    assert re.search(
        r"if\s*\(\s*!transcriptLoaderRef\s*\.\s*current\s*\)\s*\{\s*"
        r"transcriptLoaderRef\s*\.\s*current\s*=\s*makeTranscriptLoader",
        src,
    ), "the loader should be initialized once per App instance"


def test_transcript_loader_callbacks_keep_inspector_load_lifecycle():
    src = APP.read_text(encoding="utf-8")
    on_start = _extract_arrow_body(src, "onStart")
    on_success = _extract_arrow_body(src, "onSuccess")
    on_error = _extract_arrow_body(src, "onError")

    assert re.search(r"setTx\s*\(\s*null\s*\)", on_start), (
        "starting a load must clear the previous transcript"
    )
    assert re.search(r"setTranscriptError\s*\(\s*['\"]['\"]\s*\)", on_start), (
        "starting a load must clear the previous error"
    )
    assert re.search(r"setTx\s*\(\s*tx\s*\)", on_success)
    assert re.search(r"setFilename\s*\(\s*transcriptSessionIdRef\s*\.\s*current\s*\)", on_success)
    assert re.search(r"setUseSynth\s*\(\s*false\s*\)", on_success)
    assert re.search(r"setRoute\s*\(\s*['\"]session['\"]\s*\)", on_success)
    assert re.search(r"setRoute\s*\(\s*['\"]session['\"]\s*\)", on_error), (
        "the error callback must route to the Inspector"
    )
    assert re.search(
        r"console\.error\s*\(\s*['\"]transcript fetch failed['\"]\s*,\s*err\s*\)",
        on_error,
    )
    assert re.search(
        r"setTranscriptError\s*\(\s*`transcript fetch failed: \$\{message\}`\s*\)",
        on_error,
    )


def test_transcript_parse_failure_has_parse_label_and_keeps_error_object():
    src = APP.read_text(encoding="utf-8")
    on_error = re.sub(r"\s+", " ", _extract_arrow_body(src, "onError"))
    assert re.search(
        r"if\s*\(\s*kind\s*===\s*['\"]parse['\"]\s*\)\s*\{\s*"
        r"console\.error\s*\(\s*['\"]transcript parse failed['\"]\s*,\s*err\s*\)\s*;\s*"
        r"setTranscriptError\s*\(\s*`transcript parse failed: \$\{message\}`\s*\)\s*;\s*"
        r"\}\s*else\s*\{\s*"
        r"console\.error\s*\(\s*['\"]transcript fetch failed['\"]\s*,\s*err\s*\)\s*;\s*"
        r"setTranscriptError\s*\(\s*`transcript fetch failed: \$\{message\}`\s*\)\s*;\s*\}",
        on_error,
    ), "parse errors must retain their parse label and log the Error object"


def test_session_route_shows_error_before_session_view_and_ignores_reformatting():
    src = APP.read_text(encoding="utf-8")
    match = re.search(r"\{\s*route\s*===\s*'session'\s*&&\s*(\(.*?\))\s*\}", src, re.S)
    assert match, "could not locate the Inspector route render branch"
    branch = re.sub(r"\s+", " ", match.group(1)).strip()
    assert re.search(
        r"\(\s*transcriptError\s*\?\s*<div\s+className=\"err\">"
        r"\s*\{transcriptError\}\s*</div>\s*"
        r":\s*<SessionView\b",
        branch,
    ), "the error element must render for transcriptError, before the normal SessionView branch"
