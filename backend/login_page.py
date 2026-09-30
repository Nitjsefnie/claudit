"""The sign-in page: its HTML template and the response builder.

Extracted from backend/login.py — the module-size ratchet moved it here
when #395's page rework and #384's nonce slot outgrew the 500-line cap;
login.py keeps the routes and calls _login_page_response. Every slot is
a real HTML context and escaped at its own site (SV-BRAND-ESCAPE); the
inline style block carries the response's CSP nonce (#384).
"""
from __future__ import annotations

import html

from starlette.requests import Request
from starlette.responses import HTMLResponse

from backend import branding

# One answer for every credential failure — identical status and body
# for unknown id, no web password and wrong password (issue #109). The
# body is the FULL sign-in page (issue #395): the error announced in a
# role=alert element, the form still there to retry from, nothing
# user-derived echoed back — the byte-identical contract holds.
_GENERIC_FAILURE_TEXT = "Invalid credentials."
_GENERIC_LIMIT_TEXT = "Too many login attempts. Try again later."
_INVALID_UID_TEXT = "Invalid user ID"


def _login_page_response(
    request: Request, err: str, status: int
) -> HTMLResponse:
    """The sign-in page with `err` in its role=alert element.

    A credential failure or rate-limit answer used to be a bare
    text/plain line — no form to retry from, no page title, no lang
    (issue #395). Rendering the page keeps the status codes and every
    limiter semantic; only the body changes. `err` is a server-owned
    literal, never user input, so nothing from the request is echoed
    back — that is what keeps the generic bodies byte-identical (#109).

    The request rides along for its CSP nonce (#384): the inline style
    block carries it (backend.app._SecurityHeaders stashes
    scope.state.csp_nonce OUTSIDE the auth middleware, so every request
    forwarded here has it), and style-src admits the block by nonce
    instead of 'unsafe-inline'.
    """
    nonce = getattr(request.state, "csp_nonce", None)
    nonce_attr = f' nonce="{nonce}"' if nonce else ""
    return HTMLResponse(
        _LOGIN_HTML.format(
            err=err,
            app_name=html.escape(branding.brand_name().upper(), quote=True),
            style_nonce_attr=nonce_attr,
            **_privacy_notice_slots(),
        ),
        status_code=status,
    )


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<title>Sign in · {app_name}</title>
<style{style_nonce_attr}>
  body {{ background:#0b0d10; color:#dde; font-family: 'Inter',sans-serif;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; }}
  form {{ background:#14181d; padding:24px 28px; border:1px solid #25303c;
         border-radius:8px; min-width:320px; }}
  h1 {{ margin:0 0 16px 0; font-size:18px; letter-spacing:.04em; color:#9bd; }}
  label {{ display:block; margin:10px 0 4px 0; font-size:12px; color:#8aa; }}
  input {{ width:100%; box-sizing:border-box; padding:8px 10px;
          background:#0e1216; color:#dde; border:1px solid #25303c;
          border-radius:4px; font: 14px 'JetBrains Mono', monospace; }}
  button {{ margin-top:18px; width:100%; padding:10px 14px;
           background:#1f6f9c; color:#fff; border:0; border-radius:4px;
           font-weight:600; cursor:pointer; }}
  .guest-btn {{ background:#1a1c2e; border:1px solid #25303c; }}
  .guest-btn:hover {{ background:#222640; }}
  /* app.css's --muted (#7a81a8): 4.70:1 over the form card #14181d,
     above the 4.5:1 AA text floor — #556 computed 2.44:1 (issue #395). */
  .or {{ text-align:center; color:#7a81a8; font-size:11px; margin:16px 0 4px; letter-spacing:.2em; }}
  .err {{ color:#e76; font-size:12px; min-height:16px; margin-top:8px; }}
{privacy_css}</style>
</head><body>
<form method="post" action="/login">
  <h1>{app_name} · sign in</h1>
  <label for="login-user">Username</label>
  <input id="login-user" name="user_id" required inputmode="numeric" pattern="[0-9]+"
         autocomplete="username">
  <label for="login-password">Password</label>
  <input id="login-password" name="password" type="password" required
         autocomplete="current-password">
  <button type="submit">Sign in</button>
  <div class="err" role="alert">{err}</div>
  <div class="or">or</div>
  <button type="submit" class="guest-btn"
          formaction="/login/guest" formmethod="post" formnovalidate>
    Continue as guest
  </button>
{privacy_link}</form>
</body></html>
"""


def _privacy_notice_slots() -> dict[str, str]:
    """The two _LOGIN_HTML slots for the operator's privacy notice
    (issue #270). Empty unless APP_PRIVACY_NOTICE_URL is set, so the
    unset page stays byte-identical to before — no anchor, no empty
    href, and no styling either. The URL goes through
    ``branding.url_attr`` (SV-BRAND-ESCAPE): attribute-escaping alone
    cannot stop a ``javascript:`` scheme, so a value outside the
    allow-list drops the link and logs a warning (#365)."""
    url = branding.privacy_notice_url()
    if not url:
        return {"privacy_link": "", "privacy_css": ""}
    href = branding.url_attr(url)
    if href is None:
        return {"privacy_link": "", "privacy_css": ""}
    css = (
        "  .privacy-link { display:block; text-align:center;"
        " margin-top:12px; color:#8aa; font-size:12px; }\n"
    )
    link = f'<a class="privacy-link" href="{href}">Privacy notice</a>\n'
    return {"privacy_link": link, "privacy_css": css}
