"""FastAPI entrypoint for claudit."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from backend import db

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Load .env before importing backend modules that snapshot settings at import.
# db itself reads its database URLs only when a pool is first opened.
db.load_dotenv(str(_REPO_ROOT / ".env"))

# These imports follow dotenv loading because some modules capture settings
# while they are imported (for example, EXPORT_PYTHON and CLAUDIT_TIMING).
# pylint: disable=wrong-import-position
from backend import api, api_export, constants, events, ingest, login, r2, session  # noqa: E402
from backend import branding  # noqa: E402
# pylint: enable=wrong-import-position

log = logging.getLogger("claudit.app")

_PUBLIC = _REPO_ROOT / "public"
_SRC = _REPO_ROOT / "src"


def validate_bucket_config() -> None:
    """Refuse to boot on an invalid R2_BUCKET.

    r2.buckets() raises on a name that is not a valid S3 bucket name.
    Called from lifespan, that raise aborts startup — the alternative is
    the scheduler booking a fatal on the startup ingest while the server
    keeps half-serving, every transcript fetch for a mis-named bucket
    dying with a 500.
    """
    r2.buckets()


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    db.apply_schema()
    db.schema_check()
    validate_bucket_config()
    # Sweep the export tmp files a crashed process left behind (issue
    # #441): the graceful reap (#363) runs at teardown only, which a
    # SIGKILL bypasses. Aged past any live render's lifetime, so a live
    # export in a tmp-sharing process is never touched. Runs before the
    # scheduler books the startup ingest — the same startup pass.
    api_export.sweep_stale_exports()
    events.set_loop(asyncio.get_running_loop())

    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    # Hourly maintenance.
    sched.add_job(
        lambda: ingest.run_ingest(trigger="cron"),
        "cron", minute=15,
    )
    # Startup ingest: fire ASAP via a one-shot in the scheduler thread so
    # lifespan returns immediately and uvicorn starts serving. /health
    # reflects ingest state via the ingest_runs table.
    sched.add_job(
        lambda: ingest.run_ingest(trigger="startup"),
        next_run_time=datetime.now(timezone.utc),
    )
    sched.start()
    fastapi_app.state.scheduler = sched

    yield

    # Abort any in-flight ingest cooperatively (issue #103): the run stops
    # at its next bounded step, closes its ingest_runs row as aborted, and
    # skips the rebuild, the broadcast and the warm — the next successful
    # run rebuilds all derived state. Signalled FIRST, before anything
    # that can spend time: the reap below is dead time for the run, and a
    # run told to stop at once is already unwinding while it runs, rather
    # than starting to unwind after it (issue #414).
    ingest.request_shutdown()
    # A run stuck inside a long single-statement phase cannot reach a
    # bounded step (issue #372), so cancel the statements in flight: the
    # driver raises QueryCanceled, which the run classifies as the abort.
    db.cancel_viz_queries()
    # Reap live export renders (issue #363): uvicorn cancels the
    # in-flight export task at the graceful-shutdown deadline and then
    # re-raises the captured SIGTERM right after lifespan shutdown, so
    # the handler's own cleanup races process death and can lose. This is
    # the one teardown uvicorn waits for — so it draws from the stop budget
    # like every other term, and as a WHOLE rather than per live child: it
    # runs ahead of the bounded wait, and a per-child bound is drawn once
    # per child (issue #414). It runs immediately after the abort, keeping
    # only the two statements that can be skipped by a raising neighbour
    # between `yield` and it (the #363 guarantee), and the reap itself is
    # bounded, so nothing it does can strand the wait behind it.
    await api_export.reap_live_renders(constants.SHUTDOWN_RENDER_REAP_S)
    # Wake SSE generators so uvicorn's graceful-shutdown drains immediately
    # instead of waiting for the (never-ending) heartbeat response.
    events.signal_shutdown()
    sched.shutdown(wait=False)
    # Bounded so the abort's unwind (one fetch chunk + one final DB txn,
    # normally sub-second) plus uvicorn's own graceful window and the reap
    # above stays inside the unit's TimeoutStopSec: the wait is DERIVED
    # from all three, so it cannot be sized past what is left of the stop
    # budget. A wait beyond it lost the race to systemd's SIGKILL every
    # time, and the fallback below never ran (issue #410).
    # Revoke the request only when the wait succeeded: with a straggler
    # still unwinding past the timeout, revoking would let it run to
    # completion instead of aborting at its next bounded step.
    if ingest.wait_for_run(constants.SHUTDOWN_RUN_WAIT_S):
        # Nothing can start a run in this process any more, so a shutdown
        # never outlives the teardown it belongs to.
        ingest.clear_shutdown()
    else:
        # The fallback behind the bounded wait (issue #372): close the
        # open run's row, so the stop never leaves finished_at NULL. The
        # wait is sized to fit before systemd's SIGKILL, so this write
        # lands and the straggler it outlasted is what gets killed: the
        # reap ahead of it draws from the same derived budget rather than
        # from a window of its own.
        ingest.close_open_run()
        log.warning(
            "ingest still running after the shutdown wait; its row is closed "
            "as aborted and the run is left to the stop")
    events.clear_loop()


app = FastAPI(
    title=branding.brand_name(),
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
    # /api/dashboard at range=all returns ~4.5 MB (8.8k ctx_traces spanning
    # 54k turns). Starlette's JSONResponse runs jsonable_encoder over that
    # whole structure and then stdlib json.dumps — measured at 475ms + 257ms.
    # orjson serialises the same payload in 11ms.
    default_response_class=ORJSONResponse,
)


class _SelectiveGZip(GZipMiddleware):
    """GZip everything except the SSE stream.

    GZipMiddleware cannot know a streaming response's size, so it
    compresses `/api/events` unconditionally — which buys nothing (events
    are a few bytes each), risks holding them in the compressor's buffer
    instead of delivering them live, and contradicts the `no-transform`
    the endpoint already sets.
    """

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/api/events":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# The document policy both HTML surfaces get, instantiated per response
# with a fresh nonce. Measured against the real sign-in and dashboard
# pages loading under it with zero violations and panels rendering
# (issue #384):
# - script-src: unpkg.com carries the pinned, SRI-hashed React,
#   ReactDOM and Babel builds (SRI pins their bytes independently of the
#   host allowance); /src/* is 'self'; every <script> tag the dashboard
#   serves carries the response's nonce (root_index injects it), which
#   is what admits in-browser Babel: babel.min.js 7.29.0 reads each
#   text/babel source tag's nonce and sets it on the inline script
#   element it generates for the compiled output (`nonce:d.nonce` →
#   `t.nonce&&(r.nonce=t.nonce)`, verified in the pinned bytes), so one
#   per-response nonce covers the whole pipeline. The compiled text
#   itself varies per file and per Babel version, so hashes cannot cover
#   it; 'unsafe-eval' is NOT required (no new Function in the pinned
#   bytes; zero eval refusals recorded). A Babel bump re-pins the SRI
#   hash by hand — re-check the propagation there.
# - style-src: the Google Fonts stylesheet host, and the sign-in page's
#   inline <style> block by the same per-response nonce — no
#   'unsafe-inline' anywhere in the policy. React sets styles through
#   CSSOM, which CSP does not govern.
# - font-src: the Google Fonts binary host.
# - connect-src 'self': /api/* fetches, the SSE stream and Babel's XHR
#   fetches of the text/babel scripts are all same-origin.
# - img-src 'self': favicons and panel images; no data: URIs are used.
# - default-src 'none', base-uri 'none', form-action 'self' and
#   frame-ancestors 'none' are the deny-by-default floor (#369).
_CSP_TEMPLATE = (
    "default-src 'none'; "
    "script-src 'self' https://unpkg.com 'nonce-{nonce}'; "
    "style-src 'self' https://fonts.googleapis.com 'nonce-{nonce}'; "
    "font-src https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "img-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class _SecurityHeaders:
    """Full document CSP, framing protection and nosniff (#369, #384).

    The document policy (_CSP_TEMPLATE, instantiated with a fresh
    per-response nonce) plus framing protection (``X-Frame-Options:
    DENY``) ride on text/html — browsers apply the CSP and frame
    documents and ignore the headers on other types, so the sign-in
    page, the dashboard and any HTML error page all pass through here
    whatever their status. ``X-Content-Type-Options: nosniff`` rides on
    EVERY response: every content type the app serves is correct, so it
    costs nothing, and it stops a MIME-confused response from being
    reinterpreted as script or style. Headers are only filled in when
    absent, so an endpoint's own security headers and the PNG export's
    Content-Disposition are never clobbered. Added OUTSIDE the auth
    middleware: the 302 that middleware generates itself for an
    unauthenticated page load never passes an inner layer, so an inner
    placement would miss it, and the nonce it stashes in
    ``scope.state.csp_nonce`` is what the sign-in page puts on its
    inline style block.
    """

    def __init__(self, asgi_app: ASGIApp) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Scope, receive: Receive,
                       send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        await self.app(scope, receive, self._wrap_send(nonce, send))

    def _wrap_send(self, nonce: str, send: Send) -> Send:
        async def wrapped_send(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                ctype = headers.get("content-type", "")
                if ctype.split(";")[0].strip().lower() == "text/html":
                    if not headers.get("content-security-policy"):
                        headers["content-security-policy"] = (
                            _CSP_TEMPLATE.format(nonce=nonce))
                    if not headers.get("x-frame-options"):
                        headers["x-frame-options"] = "DENY"
                if not headers.get("x-content-type-options"):
                    headers["x-content-type-options"] = "nosniff"
            await send(message)
        return wrapped_send


# The origin was serving /api/dashboard uncompressed — ~3.2 MB per cold
# request, which the CDN then had to pull in full before it could
# compress and serve it on. The body is JSON and compresses ~5x.
# minimum_size skips the many small responses (/api/me, /api/models)
# where framing would cost more than it saves.
app.add_middleware(_SelectiveGZip, minimum_size=1024)
app.middleware("http")(session.auth_middleware)
# Added LAST so it is the outermost middleware: the auth middleware's
# own redirect for an unauthenticated page load never passes an inner
# layer, so only an outer placement protects every response.
app.add_middleware(_SecurityHeaders)
app.include_router(login.router)
app.include_router(api.router)


@app.get("/health")
def health() -> Response:
    parser_version = constants.PARSER_VERSION
    last_ingest = None
    try:
        with db.viz_conn() as c:
            row = c.execute(
                "SELECT id, started_at, finished_at, trigger, "
                "r2_listed, reparsed, newer, error "
                "FROM ingest_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                error = r2.redact(row[7])
                if error:
                    # Net for rows stored by older builds; new rows are
                    # count-only at the source.
                    error = re.sub(
                        r"(?s)^(\d+ objects? failed after retries):.*", r"\1", error)
                last_ingest = {
                    "id": row[0],
                    "started_at": row[1].isoformat() if row[1] else None,
                    "finished_at": row[2].isoformat() if row[2] else None,
                    "trigger": row[3],
                    "r2_listed": row[4],
                    "reparsed": row[5],
                    "newer": row[6],
                    "error": error,
                }
    except Exception:  # noqa: BLE001
        # Driver text can name hosts, databases, buckets — the public
        # body gets a generic message; the details go to the logs.
        log.exception("health: database query failed")
        # A status-code monitor (curl -fsS, an LB probe) never parses the
        # body, so failing only in the body reads as healthy to it. 503
        # carries the same JSON fields (issue #104).
        return JSONResponse(
            status_code=503,
            content={
                "ok": False, "db": False, "error": "database unavailable",
                "version": constants.VERSION,
                "parser_version": parser_version,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
    # Live progress for the run in flight. ingest_runs only gains its
    # counters in the final UPDATE, which is written only after the
    # derived-state rebuilds finish — so a caller watching that row sees
    # nothing for minutes, then "done" with the rollups already rebuilt;
    # the progress readout below is what shows the rebuilds in flight.
    prog = ingest.progress_snapshot()
    running = prog.get("phase") not in (None, "idle")
    ingest_progress = None
    if running:
        done, total = prog.get("done") or 0, prog.get("total") or 0
        ingest_progress = {
            "phase": prog.get("phase"),
            "done": done,
            "total": total,
            "pct": round(100.0 * done / total, 1) if total else None,
            "run_id": prog.get("run_id"),
            "started_at": prog.get("started_at"),
        }

    return JSONResponse(content={
        "ok": True, "db": True,
        "ingest_running": running,
        "ingest_progress": ingest_progress,
        "last_ingest": last_ingest,
        # Which build is answering. The DB-error branch above reports it
        # too: "which version is broken" is exactly the question asked when
        # /health is failing, so it must not be the field that goes missing.
        "version": constants.VERSION,
        "parser_version": parser_version,
        "now": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/admin/ingest")
# A plain def, not async: FastAPI then runs the handler on its threadpool,
# so the blocking pipeline never occupies the event loop and the service
# keeps answering (/health, /api/*, SSE) for the length of the run
# (issue #101). An `async def` here reintroduces the stall.
def admin_ingest() -> dict:
    return ingest.run_ingest(trigger="manual")


@app.get("/")
async def root_index(request: Request) -> Response:
    html = (_PUBLIC / "index.html").read_text(encoding="utf-8")
    # The default in-page snippet is `window.BACKEND_URL || ''`; when we
    # serve from the backend, set it to '/' so the frontend knows to use
    # this origin for /api/* fetches. IS_GUEST and IS_OPERATOR ride the
    # same shot, so the FIRST React render already hides the
    # guest-restricted UI and the operator-only page-performance panel
    # instead of flashing them before /api/me resolves (#629).
    is_guest = bool(getattr(request.state, "is_guest", False))
    is_operator = bool(getattr(request.state, "is_operator", False))
    # Branding rides the same injection: the page's one brand source is
    # window.BRAND {name, title, description} — from APP_NAME /
    # APP_TITLE / APP_DESCRIPTION, defaults = today's strings. `</` is
    # JS-escaped (see branding.script_json); title/meta below are
    # html-escaped instead, being real HTML contexts.
    brand_js = f"window.BRAND = {branding.script_json(branding.brand())};"
    html = html.replace(
        "<script>window.BACKEND_URL = window.BACKEND_URL || '';</script>",
        f"<script>window.BACKEND_URL = '/'; window.IS_GUEST = "
        f"{str(is_guest).lower()}; window.IS_OPERATOR = "
        f"{str(is_operator).lower()}; {brand_js}</script>",
    )
    html = branding.brand_page(html)
    # Bust intermediary caches (Cloudflare, browser) on every static-asset
    # change by appending the file's mtime to its URL. Cache lookup keys
    # by URL, so a different ?v= forces a full fetch.
    html = html.replace(
        'href="/app.css"',
        f'href="/app.css?v={int((_PUBLIC / "app.css").stat().st_mtime)}"',
    )
    # Also bust /src/* JSX/JS modules so Babel always picks up the latest,
    # and the pricing.json URL pricing-loader.js reads from its tag.
    src_root = _PUBLIC.parent / "src"

    def _bust_src(m: re.Match) -> str:
        path = m.group(1)
        try:
            v = int((src_root / path.lstrip("/").removeprefix("src/")).stat().st_mtime)
        except OSError:
            return m.group(0)
        return m.group(0).replace(path, f"{path}?v={v}")

    html = re.sub(r'(?:src|data-pricing)="(/src/[^"?]+)"', _bust_src, html)
    # Every script ELEMENT's open tag carries the response's CSP nonce
    # (kept LAST so it sees the final HTML). The two inline scripts are
    # admitted by it directly, and Babel standalone propagates a
    # text/babel source tag's nonce onto the inline script element it
    # generates for the compiled output (see _CSP_TEMPLATE), so
    # script-src needs no 'unsafe-inline'. The unpkg tags need no nonce
    # (host + SRI admit them); marking them is harmless.
    nonce = getattr(request.state, "csp_nonce", None)
    if nonce:
        html = _nonce_script_elements(html, nonce)
    return HTMLResponse(html)


# A script ELEMENT's open tag is a `<script` outside any element's
# content; inside one it is raw text that ends at the first end tag
# (issue #439). Both patterns carry the word-boundary so `<scriptx` —
# not a script element, and a shape no template should emit — is left
# alone. The end tag admits what a browser admits: anything up to the
# tag's own `>` is an attribute list to it, so `</script/>` and
# `</script foo="1">` close the element as surely as `</script>` does,
# and missing one leaves the NEXT element without its nonce.
_SCRIPT_OPEN_RE = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script\b[^>]*>", re.IGNORECASE)


def _nonce_script_elements(html: str, nonce: str) -> str:
    """Put ``nonce`` on every script ELEMENT's open tag, and only there.

    A blanket ``replace("<script", ...)`` also fires inside script
    content, where a brand value carrying the literal text `<script`
    survives ``branding.script_json`` (it cannot close the tag) and
    would receive the attribute's raw quotes inside a JS string
    literal — a SyntaxError that stops every page load (#439). The
    window.BRAND payload rides an inline script, so walking the raw
    text of each element is what keeps it out of reach.

    Conservation is the other half: removing the injected attribute
    returns the input byte for byte, tag casing included. The walk
    REWRITES a document rather than scanning it, so a tail it dropped
    would be as invisible to a reader as a tag it missed.
    """
    out: list[str] = []
    pos = 0
    while True:
        open_match = _SCRIPT_OPEN_RE.search(html, pos)
        if open_match is None:
            break
        out.append(html[pos:open_match.start()])
        out.append(f'{open_match.group(0)} nonce="{nonce}"')
        pos = open_match.end()
        # Content is raw text: it runs to the first end tag, and any
        # `<script` within it is payload, not an element. An
        # UNTERMINATED element runs to the end of the document, which
        # the trailing append below emits like any other tail.
        close_match = _SCRIPT_CLOSE_RE.search(html, pos)
        if close_match is None:
            break
        out.append(html[pos:close_match.start()])
        pos = close_match.start()
    out.append(html[pos:])
    return "".join(out)


@app.get("/app.css")
async def root_css() -> Response:
    return FileResponse(
        str(_PUBLIC / "app.css"),
        media_type="text/css",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/favicon.ico")
async def root_favicon() -> Response:
    """The tab icon: public (a browser fetches it before any sign-in),
    byte-identical to the committed public/favicon.ico -- a 32x32 ICO
    (PNG-compressed) hand-generated for the dark theme: the topbar's
    teal chevron on the --bg surface. Declared by index.html's
    <link rel="icon"> (issue #448); img-src 'self' admits the
    same-origin fetch, and no data: URI can stand in for it.
    """
    return FileResponse(
        str(_PUBLIC / "favicon.ico"),
        media_type="image/x-icon",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# /src/* is mounted via StaticFiles. The middleware gates it because the
# path doesn't start with /api or /admin and isn't in _AUTH_PUBLIC_PATHS.
app.mount("/src", StaticFiles(directory=str(_SRC)), name="src")
