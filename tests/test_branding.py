"""Branding from config: APP_NAME / APP_TITLE / APP_DESCRIPTION drive every
user-visible brand string — the FastAPI title, the served page's <title>
and meta description, the injected window.BRAND, the login page's title
and heading, the export-PNG download filename and the frontend logo.

With nothing set, every surface reproduces today's claudit strings
byte-identically. HTML contexts (title element, meta attribute, login
page) get html-escaped values; the window.BRAND payload is JSON with `</`
escaped at the JS level, so it cannot close the <script> it lives in.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import api_export
from backend import branding
from backend import login as login_mod
from backend import session as session_mod


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "public" / "index.html"


@pytest.fixture(name="page_client")
def _page_client_fixture():
    """The real backend.app, but WITHOUT lifespan (no `with`), so no DB
    is needed — `/` reads only template files and the environment. A
    guest cookie: `/` is session-gated (302 → /login without one), and a
    guest may load it, with IS_GUEST=true injected."""
    client = TestClient(app_mod.app)
    client.cookies.set(
        session_mod.SESSION_COOKIE_NAME,
        session_mod.make_guest_session_token(),
    )
    return client


@pytest.fixture(name="login_client")
def _login_client_fixture():
    a = FastAPI()
    a.include_router(login_mod.router)
    return TestClient(a)


def _diff_lines(template: str, served: str) -> list[str]:
    """+/- lines of the line diff template → served (no context)."""
    return [
        line for line in difflib.unified_diff(
            template.splitlines(), served.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


# ---------------------------------------------------------------------------
# Served index page
# ---------------------------------------------------------------------------


def test_default_page_differs_from_template_only_by_injections(page_client):
    """Nothing set: today's page, byte-identical, except the injected
    window.BRAND (which rides the same <script> line as BACKEND_URL) and
    the mtime cache-busts the page always carried."""
    served = page_client.get("/").text
    template = INDEX.read_text(encoding="utf-8")
    ops = _diff_lines(template, served)
    assert ops, "the rewrite must touch the template"
    for line in ops:
        payload = line[1:]
        if line.startswith("+"):
            assert any(k in payload for k in (
                "window.BACKEND_URL = '/';", "window.BRAND = {", "?v="
            )), f"unexpected served-line change: {payload}"
        else:
            assert any(k in payload for k in (
                "window.BACKEND_URL = window.BACKEND_URL",
                'href="/app.css"', 'src="/src/'
            )), f"unexpected template-line change: {payload}"


def test_default_page_injects_default_brand(page_client):
    served = page_client.get("/").text
    assert (
        'window.BRAND = {"name": "claudit", '
        '"title": "claudit · Claude Code Usage Dashboard", '
        '"description": "Self-hosted dashboards for Claude Code session '
        'JSONL: cost-by-model, token breakdown, prompt-cache TTL split, '
        'response sizes, per-session context growth, session burn rate, '
        'tool-usage ratio, reply latency."}'
    ) in served
    assert "<title>claudit · Claude Code Usage Dashboard</title>" in served


def test_env_overrides_served_title_meta_and_brand(page_client, monkeypatch):
    monkeypatch.setenv("APP_NAME", "codexmeter")
    monkeypatch.setenv("APP_TITLE", "Codexmeter <x>")
    monkeypatch.setenv("APP_DESCRIPTION", 'He said "hi" & left')
    served = page_client.get("/").text
    assert "<title>Codexmeter &lt;x&gt;</title>" in served
    assert 'content="He said &quot;hi&quot; &amp; left"' in served
    assert 'window.BRAND = {"name": "codexmeter", "title": "Codexmeter <x>"' in served


def test_brand_payload_cannot_close_its_script_tag(page_client, monkeypatch):
    monkeypatch.setenv("APP_TITLE", 'a</script><b>')
    served = page_client.get("/").text
    assert 'a<\\/script><b>' in served
    # The only literal </script>s left are the template's own tag closes.
    assert served.count("</script>") == INDEX.read_text(
        encoding="utf-8").count("</script>")


def test_brand_rides_the_existing_injection_script(page_client):
    served = page_client.get("/").text
    # The guest cookie the fixture set: IS_GUEST comes out true, and
    # window.BRAND rides the SAME <script> line as BACKEND_URL/IS_GUEST.
    assert (
        "<script>window.BACKEND_URL = '/'; window.IS_GUEST = true; "
        "window.BRAND = {" in served
    )


# ---------------------------------------------------------------------------
# FastAPI / OpenAPI title
# ---------------------------------------------------------------------------


def test_fastapi_title_follows_env():
    """The FastAPI title is read at import; each case boots a fresh
    interpreter (no lifespan, no DB) and prints app.title."""
    code = "import backend.app as m; print(m.app.title)"
    base = {k: v for k, v in os.environ.items() if not k.startswith("APP_")}
    for env_name, want in (("APP_NAME", "codexmeter"), ("APP_UNSET_X", None)):
        env = dict(base)
        if want is not None:
            env[env_name] = want
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env,
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == ("codexmeter" if want else "claudit")


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


def test_login_page_default_is_byte_identical(login_client, monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    html = login_client.get("/login").text
    assert "<title>Sign in · CLAUDIT</title>" in html
    assert "<h1>CLAUDIT · sign in</h1>" in html


def test_login_page_follows_app_name(login_client, monkeypatch):
    monkeypatch.setenv("APP_NAME", "codexmeter")
    html = login_client.get("/login").text
    assert "<title>Sign in · CODEXMETER</title>" in html
    assert "<h1>CODEXMETER · sign in</h1>" in html


def test_login_page_escapes_the_name(login_client, monkeypatch):
    monkeypatch.setenv("APP_NAME", 'a<b>&"c')
    html = login_client.get("/login").text
    assert "<title>Sign in · A&lt;B&gt;&amp;&quot;C</title>" in html
    assert "<h1>A&lt;B&gt;&amp;&quot;C · sign in</h1>" in html


# ---------------------------------------------------------------------------
# Export-PNG download filename
# ---------------------------------------------------------------------------


def test_export_filename_default(monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    assert api_export.export_filename("30d", "proj/one") == (
        "claudit_proj_one_30d.png")


def test_export_filename_follows_brand(monkeypatch):
    monkeypatch.setenv("APP_NAME", "codexmeter")
    assert api_export.export_filename("30d", "proj/one") == (
        "codexmeter_proj_one_30d.png")
    monkeypatch.setenv("APP_NAME", "My Meter")  # slugified like project
    assert api_export.export_filename("all", None) == "My_Meter_all_all.png"


def test_export_filename_never_leads_with_underscore(monkeypatch):
    """APP_NAME set but empty used to export "_all_all.png"; an empty
    or whitespace-only value must fall back to the default name."""
    monkeypatch.setenv("APP_NAME", "")
    assert api_export.export_filename("all", None) == (
        f"{branding.DEFAULT_NAME}_all_all.png")
    monkeypatch.setenv("APP_NAME", "   ")
    assert api_export.export_filename("30d", "proj/one") == (
        f"{branding.DEFAULT_NAME}_proj_one_30d.png")


# ---------------------------------------------------------------------------
# Frontend source-level guards
# ---------------------------------------------------------------------------


def test_app_jsx_has_no_literal_brand():
    """The brand reaches app.jsx only through window.BRAND; a literal
    CLAUDIT / claudit_ there is a regression to the hardcoded name."""
    src = (ROOT / "src" / "app.jsx").read_text(encoding="utf-8")
    src = re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)
    assert not re.search(r"CLAUDIT", src)
    assert not re.search(r"claudit_", src)
    assert re.search(r"window\.BRAND", src), "brand must come from the page"


def test_script_json_escapes_close_tag():
    assert branding.script_json({"a": "x</script>y"}) == (
        '{"a": "x<\\/script>y"}')


def test_script_json_escapes_every_script_unsafe_sequence():
    """Beyond `</`: `<!--` opens a script-hiding HTML comment, and raw
    U+2028/U+2029 are JS line terminators inside a string literal even
    though JSON treats them as ordinary whitespace. The `<\\!--` output
    is a JS string escape, deliberately not JSON-valid (`\\!` is no JSON
    escape): the payload is consumed as a JS object literal
    (src/app.jsx's `window.BRAND`), never JSON.parse'd."""
    assert branding.script_json("a b c") == (
        '"a\\u2028b\\u2029c"')
    assert "<!--" not in branding.script_json("x<!--y")
    assert branding.script_json("x<!--y") == '"x<\\!--y"'


def test_brand_payload_through_the_page_survives_hostile_title(
        page_client, monkeypatch):
    """End to end: every script-unsafe sequence in an env value is
    escaped by the time the page is served."""
    monkeypatch.setenv("APP_TITLE", 'a</script>b<!--c d')
    served = page_client.get("/").text
    assert "a<\\/script>b<\\!--c\\u2028d" in served


def test_brand_defaults_are_todays_strings():
    assert branding.DEFAULT_NAME == "claudit"
    assert branding.DEFAULT_TITLE == "claudit · Claude Code Usage Dashboard"
    assert branding.DEFAULT_DESCRIPTION == (
        "Self-hosted dashboards for Claude Code session JSONL: "
        "cost-by-model, token breakdown, prompt-cache TTL split, response "
        "sizes, per-session context growth, session burn rate, "
        "tool-usage ratio, reply latency.")


def test_empty_or_whitespace_brand_values_fall_back_to_defaults(monkeypatch):
    """APP_NAME set but EMPTY renders an empty logo and exports
    "_all_all.png": a value that is empty or whitespace-only must read
    as unset for all three brand strings."""
    for value in ("", "   "):
        monkeypatch.setenv("APP_NAME", value)
        monkeypatch.setenv("APP_TITLE", value)
        monkeypatch.setenv("APP_DESCRIPTION", value)
        assert branding.brand() == {
            "name": branding.DEFAULT_NAME,
            "title": branding.DEFAULT_TITLE,
            "description": branding.DEFAULT_DESCRIPTION,
        }
