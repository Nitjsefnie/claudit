"""The Overview must never sit on "loading…" once loading has ended.

Issue #394: `src/app.jsx` fetched /api/dashboard with no `r.ok` check, its
catch only logged, and the aggregate builder returned null for a range
with no rows -- so BOTH an empty range and a 500 rendered the same
"status: loading…" placeholder, forever.

Nothing else in the suite can see this: no test renders React and node
cannot parse JSX. So the decision logic is extracted into
`src/dashboard-fetch.js` -- plain JS, no React, no fetch glue -- and
driven through node here, while test_panel_wiring.py pins that app.jsx
actually WIRES it (a correct module nobody calls passes this file).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FETCH_JS = ROOT / "src" / "dashboard-fetch.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(body: str):
    """Run `body` against the real src/dashboard-fetch.js in node."""
    script = f"""
      global.window = {{}};
      global.fetch = undefined;
      require({str(FETCH_JS)!r});
      {body}
    """
    # Over STDIN, not -e: the payload below embeds JSON, and Windows
    # refuses a CreateProcess command line over 32k (WinError 206).
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- the state machine -------------------------------------------------

def test_states_are_tracked_apart_from_the_data():
    """A request in flight, a landed request and a failed request are three
    different things, and none of them is "the data is null"."""
    out = _node("""
      const F = window.dashboardFetch;
      console.log(JSON.stringify({
        loading: F.start(),
        ready: F.loaded(),
        error: F.failed('HTTP 500'),
      }));
    """)
    assert out["loading"] == {"status": "loading", "detail": ""}
    assert out["ready"] == {"status": "ready", "detail": ""}
    assert out["error"] == {"status": "error", "detail": "HTTP 500"}


def test_failed_without_a_message_still_names_itself():
    """A rejection with an empty message must not render a blank error."""
    out = _node("console.log(JSON.stringify(window.dashboardFetch.failed('')));")
    assert out["detail"], "an error state with no detail renders as nothing"


def test_loading_and_empty_and_error_summaries_differ():
    """The bug in one assertion: every unresolved case said loading…."""
    out = _node("""
      const F = window.dashboardFetch;
      const s = st => F.summary(st, false);
      console.log(JSON.stringify({
        loading: s(F.start()),
        empty: s(F.loaded()),
        error: s(F.failed('HTTP 503')),
      }));
    """)
    texts = {k: v["text"] for k, v in out.items()}
    assert out["loading"]["kind"] == "loading"
    assert out["empty"]["kind"] == "empty"
    assert out["error"]["kind"] == "error"
    assert out["error"]["detail"] == "HTTP 503", "an error must show the status"
    assert len(set(texts.values())) == 3, f"states collide on text: {texts}"
    assert "loading" not in texts["empty"].lower(), (
        "a landed-but-empty response still says loading")


def test_empty_summary_uses_the_other_panels_wording():
    """'no ... data in range' is what every other panel already says."""
    out = _node("""
      const F = window.dashboardFetch;
      console.log(JSON.stringify(F.summary(F.loaded(), false)));
    """)
    assert out["text"] == "no usage data in range"


def test_data_replaces_every_placeholder():
    """With a rendered shape the summary is the stat block, not a status."""
    out = _node("""
      const F = window.dashboardFetch;
      console.log(JSON.stringify({
        ready: F.summary(F.loaded(), true),
        // An error on top of data the PREVIOUS response left on screen:
        // the stale numbers are still drawn, so the failure has to show.
        stale: F.summary(F.failed('HTTP 500'), true),
        loading: F.summary(F.start(), true),
      }));
    """)
    assert out["ready"] == {"kind": "data", "text": "", "detail": "",
                            "error": False}
    assert out["stale"]["kind"] == "data", "a refetch failure must not blank the page"
    assert out["stale"]["error"] is True, "but it must still be announced"
    assert out["stale"]["detail"] == "HTTP 500"
    assert out["loading"] == {"kind": "data", "text": "", "detail": "",
                              "error": False}


# --- the request itself ------------------------------------------------

def test_load_parses_a_successful_response():
    out = _node("""
      const F = window.dashboardFetch;
      const stub = () => Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({hourly: [{hour: '2026-09-30T00:00:00Z'}]}),
      });
      F.load('/api/dashboard?range=1d', {signal: 'S'}, stub)
        .then(b => console.log(JSON.stringify({rows: b.hourly.length})));
    """)
    assert out["rows"] == 1


def test_load_carries_the_run_signal_and_credentials():
    """The supersede guard (issue #179) is the abort signal; dropping it
    lets a stale response overwrite a fresher one."""
    out = _node("""
      const F = window.dashboardFetch;
      let seen = null;
      const stub = (url, init) => {
        seen = init;
        return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})});
      };
      F.load('/api/dashboard?range=1d', {signal: 'SIG'}, stub)
        .then(() => console.log(JSON.stringify({
          signal: seen.signal, creds: seen.credentials})));
    """)
    assert out["signal"] == "SIG"
    assert out["creds"] == "same-origin"


def test_load_raises_naming_the_status_on_a_500_text_body():
    """A 500 is text/plain, so the body cannot be trusted to parse."""
    out = _node("""
      const F = window.dashboardFetch;
      const stub = () => Promise.resolve({
        ok: false, status: 500,
        json: () => Promise.reject(new SyntaxError('Unexpected token < in JSON')),
      });
      F.load('/api/dashboard', {}, stub)
        .then(() => console.log(JSON.stringify({msg: null})),
              e => console.log(JSON.stringify({msg: e.message})));
    """)
    assert out["msg"] and "500" in out["msg"], out


def test_load_surfaces_a_json_detail_field():
    """A 503 body is JSON with a `detail` -- show it, as the transcript
    loader already does."""
    out = _node("""
      const F = window.dashboardFetch;
      const stub = () => Promise.resolve({
        ok: false, status: 503,
        json: () => Promise.resolve({detail: 'database is down'}),
      });
      F.load('/api/dashboard', {}, stub)
        .then(() => console.log(JSON.stringify({msg: null})),
              e => console.log(JSON.stringify({msg: e.message})));
    """)
    assert out["msg"] == "HTTP 503: database is down", out


def test_load_rejects_rather_than_returning_a_failed_response():
    """An `if (!r.ok)` check that only logs leaves the caller holding a
    body it cannot use; load must either resolve with the payload or
    reject."""
    out = _node("""
      const F = window.dashboardFetch;
      const stub = () => Promise.resolve({
        ok: false, status: 500,
        json: () => Promise.reject(new SyntaxError('nope')),
      });
      F.load('/api/dashboard', {}, stub).then(
        b => console.log(JSON.stringify({resolved: b})),
        e => console.log(JSON.stringify({rejected: e.message})));
    """)
    assert "rejected" in out, f"a 500 resolved instead of rejecting: {out}"
