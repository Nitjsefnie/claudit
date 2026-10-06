"""No parser stores a placeholder model, and a model-less file refuses (issue #688).

`unknown` and `(unknown)` are placeholders for a model that was never
actually unknown. Under the #653 ruling no parser may emit one: the
Claude path leaves a model-less usage-bearing line model-less (None),
and parse_file's refusal — shared with the lanes since #653 — fails the
file loudly, storing no rows. The browser mirror keeps null the same
way.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from backend import db, ingest, parse
from backend.parse_lanes import refuse_unattributed
from tests import scratch_db

ROOT = Path(__file__).resolve().parents[1]
FIX_PARSER = ROOT / "fixtures" / "parser"
FIX_CODEX = ROOT / "fixtures" / "codex"
PARSER_JS = ROOT / "src" / "parser.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"

# The node skip rides only the one test that runs node (#743): the
# backend pins below must hold wherever pytest runs, node or not.
PLACEHOLDERS = ("unknown", "(unknown)")


def test_no_parser_fixture_carries_a_placeholder_model():
    """Every parser's fixtures, swept through the shared entry point,
    parse to rows carrying a real model. A fixture the entry point
    REFUSES stores nothing and passes: that refusal is #653's, already
    pinned in test_parse_codex."""
    for sub in (FIX_PARSER, FIX_CODEX):
        for path in sorted(sub.glob("*.jsonl")):
            try:
                out = parse.parse_file(f"{sub.name}/{path.name}",
                                       path.read_bytes())
            except ValueError:
                continue  # refused: stores no rows, names no model
            for r in out["records"] + out["tool_uses"]:
                m = r.get("model")
                assert m and m not in PLACEHOLDERS, (
                    f"{sub.name}/{path.name} line {r.get('line_num')}: {m!r}")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not available")
def test_a_browser_claude_parse_keeps_a_modelless_record_null():
    """The browser mirror (issue #688): a model-less assistant usage line
    parses to a null model — never the `(unknown)` placeholder, which
    the backend now refuses."""
    text = (
        '{"type":"assistant","timestamp":"2026-05-07T10:00:01Z",'
        '"uuid":"u-nm","requestId":"req-nm",'
        '"message":{"role":"assistant","content":[{"type":"text",'
        '"text":"r"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
    )
    script = f"""
      global.window = {{}};
      require({str(LOADER_JS)!r}); require({str(PARSER_JS)!r});
      const {{ meta }} = window.parseTranscript({json.dumps(text)});
      console.log(JSON.stringify(
        meta.filter(m => m.type === 'assistant_usage').map(m => m.model)));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True,
        timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    models = json.loads(proc.stdout)
    assert models == [None]


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB."""
    yield from scratch_db.scratch_viz_database(monkeypatch, "placeholder_model")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env_fixture(monkeypatch):
    """The mini mirror, copied to a temp dir the ingest reads."""
    src = ROOT / "fixtures" / "r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-placeholder-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "claude"
    shutil.rmtree(tmp)


def test_a_modelless_file_is_refused_and_stores_no_rows(
        fresh_db, mini_r2_env):
    """A Claude transcript whose usage-bearing assistant line names no
    model refuses the FILE, loudly, at parse — one failed object, no
    files/records/tool_uses rows for it — while every well-formed file
    of the same run still ingests. The refusal is per-file, never
    run-fatal."""
    key = "claude/projNM/sessNM/sessNM.jsonl"
    (mini_r2_env / "projNM" / "sessNM").mkdir(parents=True)
    (mini_r2_env / "projNM" / "sessNM" / "sessNM.jsonl").write_bytes(
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z",'
        b'"uuid":"u-nm","requestId":"req-nm","sessionId":"sessNM",'
        b'"message":{"role":"assistant","content":[{"type":"text",'
        b'"text":"r"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n')

    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 1
    assert result["error"] == "1 object failed after retries"
    with db.viz_conn() as c:
        # Row-tuple compares: None-safe where fetchone() can answer None.
        assert c.execute(
            "SELECT COUNT(*) FROM files WHERE file_key = %s",
            (key,)).fetchone() == (0,)
        assert c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key = %s",
            (key,)).fetchone() == (0,)
        assert c.execute(
            "SELECT COUNT(*) FROM tool_uses WHERE file_key = %s",
            (key,)).fetchone() == (0,)
        # The well-formed mirror files of the same run still store.
        assert c.execute(
            "SELECT COUNT(*) FROM files WHERE file_key <> %s",
            (key,)).fetchone() != (0,)


def test_the_refusal_names_both_placeholder_spellings():
    """Every branch of the refusal's predicate, discriminated directly
    (#688): a falsy model, the lane spelling and Claude's historical
    parenthesised sentinel each refuse — and a real model (plus the
    <synthetic> stub, which #563/#688 keep OUT of the refusal) passes
    through unchanged."""
    for fmt, row in (("claude", {"model": None}),
                     ("claude", {"model": ""}),
                     ("codex", {"model": "unknown"}),
                     ("kimi-code", {"model": "(unknown)"})):
        with pytest.raises(ValueError):
            refuse_unattributed({"records": [dict(row)], "tool_uses": []},
                                fmt, "k")
    ok = {"records": [{"model": "claude-sonnet-4-5"}],
          "tool_uses": [{"model": "<synthetic>"}]}
    assert refuse_unattributed(ok, "claude", "k") is ok
