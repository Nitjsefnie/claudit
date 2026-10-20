"""FastAPI entrypoint for claudit."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
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
    Response,
)
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from backend import db, env_config

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Load .env before importing backend modules that snapshot settings at import.
# db itself reads its database URLs only when a pool is first opened.
db.load_dotenv(str(_REPO_ROOT / ".env"))

# These imports follow dotenv loading because some modules capture settings
# while they are imported (for example, CLAUDIT_EXPORT_PYTHON and CLAUDIT_TIMING).
# pylint: disable=wrong-import-position
from backend import api, agent_types, api_export, constants, events, ingest, login, r2, session  # noqa: E402
from backend import branding, flags  # noqa: E402
from backend.feature_payloads import features_script  # noqa: E402
from backend.ingest_progress import health  # noqa: E402
from backend.source_files import MinifiedStaticFiles  # noqa: E402
from backend.source_asset_identity import source_transform_identity  # noqa: E402
# pylint: enable=wrong-import-position

log = logging.getLogger("claudit.app")

_PUBLIC = _REPO_ROOT / "public"
_SRC = _REPO_ROOT / "src"
_VENDOR = _PUBLIC / "vendor"
# Keep classic scripts executable under nosniff on every host MIME registry.
mimetypes.add_type("text/javascript", ".js")


def validate_bucket_config() -> None:
    """Refuse to boot on an invalid CLAUDIT_R2_BUCKET.

    r2.buckets() raises on a name that is not a valid S3 bucket name.
    Called from lifespan, that raise aborts startup — the alternative is
    the scheduler booking a fatal on the startup ingest while the server
    keeps half-serving, every transcript fetch for a mis-named bucket
    dying with a 500.
    """
    r2.buckets()


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    env_config.warn_deprecated_env(log)
    db.apply_schema()
    db.schema_check()
    validate_bucket_config()
    # Sweep aged export tmp files before startup ingest. SIGKILL bypasses
    # the graceful teardown reap; the age bound protects live renders in
    # processes sharing tmp.
    api_export.sweep_stale_exports()
    events.set_loop(asyncio.get_running_loop())

    sched = BackgroundScheduler(daemon=True, timezone="UTC")
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

    # Abort any in-flight ingest cooperatively: the run stops
    # at its next bounded step, closes its ingest_runs row as aborted, and
    # skips the rebuild, the broadcast and the warm — the next successful
    # run rebuilds all derived state. Signalled FIRST, before anything
    # that can spend time: the reap below is dead time for the run, and a
    # run told to stop at once is already unwinding while it runs, rather
    # than starting to unwind after it.
    ingest.request_shutdown()
    # A run stuck inside a long single-statement phase cannot reach a
    # bounded step, so cancel the statements in flight: the
    # driver raises QueryCanceled, which the run classifies as the abort.
    db.cancel_viz_queries()
    # Reap live renders during the lifespan shutdown uvicorn awaits:
    # export-task cleanup can lose the race to SIGTERM. One shared deadline
    # bounds all children, preserving the subsequent ingest wait's budget.
    # Run immediately after abort and cancellation, so a raising neighbour
    # cannot skip the reap or strand the wait behind it.
    await api_export.reap_live_renders(constants.SHUTDOWN_RENDER_REAP_S)
    # Wake SSE generators so uvicorn's graceful-shutdown drains immediately
    # instead of waiting for the (never-ending) heartbeat response.
    events.signal_shutdown()
    sched.shutdown(wait=False)
    # Derive the ingest wait from the stop budget after uvicorn's graceful
    # window and the render reap, leaving time for the fallback row close
    # before systemd SIGKILL. Revoke abort only after a successful wait:
    # revoking with a straggler would let it complete rather than abort.
    if ingest.wait_for_run(constants.SHUTDOWN_RUN_WAIT_S):
        # Nothing can start a run in this process any more, so a shutdown
        # never outlives the teardown it belongs to.
        ingest.clear_shutdown()
    else:
        # The fallback behind the bounded wait: close the
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
    # Use orjson for large dashboard payloads to avoid JSONResponse's
    # recursive jsonable_encoder pass and stdlib serialization cost.
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
# with a fresh nonce:
# - script-src: /vendor/* carries the pinned, SRI-hashed React,
#   ReactDOM and Babel builds; /vendor/* and /src/* are 'self'. SRI
#   pins the vendored bytes independently; every <script> tag the dashboard
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
#   frame-ancestors 'none' are the deny-by-default floor.
_CSP_TEMPLATE = (
    "default-src 'none'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' https://fonts.googleapis.com 'nonce-{nonce}'; "
    "font-src https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "img-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class _SecurityHeaders:
    """Full document CSP, framing protection and nosniff.

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
    Content-Disposition are never clobbered. Placed OUTSIDE the auth
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


# Compress large JSON responses at the origin so the CDN need not
# fetch full uncompressed bodies. minimum_size skips small responses
# where framing costs more than it saves. Level 6 limits event-loop CPU
# without spending the default level 9's cost for a small size gain.
app.add_middleware(_SelectiveGZip, minimum_size=1024, compresslevel=6)
app.middleware("http")(session.auth_middleware)
# Added LAST so it is the outermost middleware: the auth middleware's
# own redirect for an unauthenticated page load never passes an inner
# layer, so only an outer placement protects every response.
app.add_middleware(_SecurityHeaders)
app.include_router(login.router)
app.include_router(api.router)
app.get("/health")(health)


@app.post("/admin/ingest")
# Use a synchronous handler so FastAPI runs blocking ingest in its
# threadpool and keeps health, API and SSE responsive throughout the run.
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
    # instead of flashing them before /api/me resolves.
    is_guest = bool(getattr(request.state, "is_guest", False))
    is_operator = bool(getattr(request.state, "is_operator", False))
    # Inject one brand source, window.BRAND {name, title, description},
    # from the CLAUDIT_APP_* settings and their defaults. script_json
    # JS-escapes </; title/meta take HTML escaping at their own contexts.
    brand_js = f"window.BRAND = {branding.script_json(branding.brand())};"
    html = html.replace(
        "<script>window.BACKEND_URL = window.BACKEND_URL || '';</script>",
        f"<script>window.BACKEND_URL = '/'; window.IS_GUEST = "
        f"{str(is_guest).lower()}; window.IS_OPERATOR = "
        f"{str(is_operator).lower()}; {brand_js} {agent_types.fold_js()}{features_script(flags.CONFIG)}</script>",
    )
    html = branding.brand_page(html)
    # Bust intermediary caches (Cloudflare, browser) on every static-asset
    # change by appending the file's mtime to its URL. Cache lookup keys
    # by URL, so a different ?v= forces a full fetch.
    html = html.replace(
        'href="/app.css"',
        f'href="/app.css?v={int((_PUBLIC / "app.css").stat().st_mtime)}"',
    )
    # Also bust /src/* JSX/JS modules, the pricing.json URL read from its
    # tag, and /vendor/* scripts so new bytes always match their SRI pins.
    asset_root = _PUBLIC.parent

    def _bust_asset(m: re.Match) -> str:
        path = m.group(1)
        try:
            asset = (_VENDOR / path.removeprefix("/vendor/")
                     if path.startswith("/vendor/") else asset_root / path.lstrip("/"))
            v = int(asset.stat().st_mtime)
        except OSError:
            return m.group(0)
        identity = f"&amp;transform={source_transform_identity()}" if path.startswith("/src/") else ""
        return m.group(0).replace(path, f"{path}?v={v}{identity}")

    html = re.sub(r'(?:src|data-pricing)="(/(?:src|vendor)/[^"?]+)"', _bust_asset, html)
    # Every script ELEMENT's open tag carries the response's CSP nonce
    # (kept LAST so it sees the final HTML). The two inline scripts are
    # admitted by it directly, and Babel standalone propagates a
    # text/babel source tag's nonce onto the inline script element it
    # generates for the compiled output (see _CSP_TEMPLATE), so
    # script-src needs no 'unsafe-inline'. The external script files are
    # also admitted by 'self'.
    nonce = getattr(request.state, "csp_nonce", None)
    if nonce:
        html = _nonce_script_elements(html, nonce)
    return HTMLResponse(html)


# A script element opener is outside element content; content ends
# at the first closing script tag. Word boundaries exclude scriptx.
# Allow browser-valid closing attributes, including / and foo="1":
# missing an admitted close would leave the next element without a nonce.
_SCRIPT_OPEN_RE = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script\b[^>]*>", re.IGNORECASE)


def _nonce_script_elements(html: str, nonce: str) -> str:
    """Put ``nonce`` on every script ELEMENT's open tag, and only there.

    A blanket ``replace("<script", ...)`` also fires inside script
    content, where a brand value carrying the literal text `<script`
    survives ``branding.script_json`` (it cannot close the tag) and
    would receive the attribute's raw quotes inside a JS string
    literal — a SyntaxError that stops every page load. The
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
    """The tab icon: public (a browser fetches it before any sign-in);
    img-src 'self' admits the same-origin fetch, and no data: URI can
    stand in for it."""
    return FileResponse(
        str(_PUBLIC / "favicon.ico"),
        media_type="image/x-icon",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# /src/* and /vendor/* use StaticFiles. The middleware gates both because the
# path doesn't start with /api or /admin and isn't in _AUTH_PUBLIC_PATHS.
app.mount("/src", MinifiedStaticFiles(directory=str(_SRC)), name="src")
app.mount("/vendor", StaticFiles(directory=str(_VENDOR)), name="vendor")
