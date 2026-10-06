"""One role, one name in the Inspector too (issue #691).

The backend folds an agent role to its canonical name at parse time
(backend/agent_types.py, issue #650): files.agent_type and the dispatch
columns store it, and Cost by Agent Type reports it. The Inspector's
agent_spawn events were built from the dispatch's RAW argument
(c.input.name / c.input.subagent_type), so a transcript and its stored
columns disagreed on one screen.

The fold TABLE stays backend-owned (SV-WHY-COLUMNS' canonical-name
paragraph; the #691 brief's constraint: no second literal table in the
browser). The page injects it as window.AGENT_TYPE_FOLD at serve time
(backend/agent_types.fold_js, riding app.py's existing injection script),
and the browser lookup (src/agent-types.js) holds the split+lookup logic
only. Pinned three ways: browser output == backend canonical_agent_type
over the fold's whole case set (node), the served page carries the table
(TestClient over the real app), and index.html loads the helper before
the parser that calls it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import agent_types
from backend import app as app_mod
from backend import session as session_mod

ROOT = Path(__file__).resolve().parents[1]
AGENT_TYPES_JS = ROOT / "src" / "agent-types.js"
PARSER_JS = ROOT / "src" / "parser.js"
RECORD_DEDUP_JS = ROOT / "src" / "record-dedup.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# Every branch of the fold: plugin namespace, a nested one, canonical
# passthrough, both cross-lane spellings, the default bucket, an unknown
# role, and no name at all (the parser's '?' fallback).
CASES = [
    "superpowers:code-reviewer",
    "deep:stack:reviewer",
    "Explore",
    "explore",
    "explorer",
    "coder",
    "worker",
    "general-purpose",
    "custom-role",
    None,
]


def _agent_line(role: str | None) -> str:
    """One assistant line dispatching one agent, Claude-jsonl shape (the
    mirror test's _claude_line shape, with an Agent tool_use)."""
    tool_use = {
        "type": "tool_use", "id": f"tu-{role}", "name": "Agent",
        "input": ({"name": role, "prompt": "go"} if role
                  else {"prompt": "go"}),
    }
    message = {
        "role": "assistant",
        "content": [tool_use, {"type": "text", "text": "dispatch"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return json.dumps(
        {"type": "assistant", "timestamp": "2026-10-06T10:00:00Z",
         "uuid": f"u-{role}", "requestId": "r-1", "sessionId": "sessS",
         "message": message},
        separators=(",", ":"),
    ) + "\n"


def _node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True,
        timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _folded_names(text: str) -> list[str]:
    """agent_spawn agent_names out of the real browser parse path, with
    the served table in place."""
    script = f"""
      global.window = {{}};
      require({str(AGENT_TYPES_JS)!r});
      window.AGENT_TYPE_FOLD = {json.dumps(dict(agent_types._FOLD))};
      require({str(RECORD_DEDUP_JS)!r});
      require({str(LOADER_JS)!r});
      require({str(PARSER_JS)!r});
      const {{ events }} = window.parseTranscript({json.dumps(text)}, {{}});
      console.log(JSON.stringify(
        events.filter(e => e.type === 'agent_spawn').map(e => e.agent_name)));
    """
    return json.loads(_node(script))


@pytest.fixture(name="page_client")
def _page_client_fixture():
    """The real backend.app WITHOUT lifespan (no DB), guest cookie set:
    `/` is session-gated and a guest may load it (the branding fixture's
    shape)."""
    client = TestClient(app_mod.app)
    client.cookies.set(
        session_mod.SESSION_COOKIE_NAME,
        session_mod.make_guest_session_token(),
    )
    return client


def test_browser_fold_matches_backend_over_the_case_set():
    """Every case the backend fold answers, the browser fold answers the
    same through the real parseTranscript path."""
    text = "".join(_agent_line(role) for role in CASES)
    assert _folded_names(text) == [
        agent_types.canonical_agent_type(role if role is not None else "?")
        for role in CASES
    ]


def test_a_subagent_type_only_dispatch_folds_the_same():
    """The Kimi-shaped argument (subagent_type, no name) folds through the
    same helper, not around it."""
    tool_use = {
        "type": "tool_use", "id": "tu-1", "name": "Task",
        "input": {"subagent_type": "superpowers:code-reviewer"},
    }
    message = {
        "role": "assistant",
        "content": [tool_use],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    line = json.dumps(
        {"type": "assistant", "timestamp": "2026-10-06T10:00:00Z",
         "uuid": "u-1", "requestId": "r-1", "sessionId": "sessS",
         "message": message},
        separators=(",", ":"),
    ) + "\n"
    script = f"""
      global.window = {{}};
      require({str(AGENT_TYPES_JS)!r});
      window.AGENT_TYPE_FOLD = {json.dumps(dict(agent_types._FOLD))};
      require({str(RECORD_DEDUP_JS)!r});
      require({str(LOADER_JS)!r});
      require({str(PARSER_JS)!r});
      const {{ events }} = window.parseTranscript({json.dumps(line)}, {{}});
      console.log(JSON.stringify(
        events.filter(e => e.type === 'agent_spawn').map(e => e.agent_name)));
    """
    assert json.loads(_node(script)) == ["code-reviewer"]


def test_without_the_served_table_the_lookup_degrades_to_the_namespace_split():
    """No injected table (a direct file:// open, a test harness that does
    not serve one): the namespace split still applies and unknown roles
    pass through -- a missing table degrades the fold, never the display."""
    script = f"""
      global.window = {{}};
      require({str(AGENT_TYPES_JS)!r});
      console.log(JSON.stringify([
        window.canonicalAgentType('deep:stack:code-reviewer'),
        window.canonicalAgentType('explore'),
        window.canonicalAgentType('custom-role'),
      ]));
    """
    assert json.loads(_node(script)) == ["code-reviewer", "explore",
                                         "custom-role"]


def test_index_injects_the_served_fold_table(page_client):
    """The served `/` carries window.AGENT_TYPE_FOLD with the backend
    fold's exact content, riding the same injection script the guest and
    brand flags ride."""
    body = page_client.get("/").text
    match = re.search(r"window\.AGENT_TYPE_FOLD = (\{.*?\});", body)
    assert match, body
    assert json.loads(match.group(1)) == dict(agent_types._FOLD)


def test_index_loads_the_helper_before_the_parser():
    """script order in index.html: the helper is defined before parser.js
    calls it at parse time."""
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    helper_at = html.index('src="/src/agent-types.js"')
    parser_at = html.index('src="/src/parser.js"')
    assert helper_at < parser_at
