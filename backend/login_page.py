"""Sign-in HTML template and response builder.

Escape every slot at its HTML context (SV-BRAND-ESCAPE); the inline style
carries the response CSP nonce. login owns the routes.
"""
from __future__ import annotations

import html

from starlette.requests import Request
from starlette.responses import HTMLResponse

from backend import branding
from backend.guest_login import guest_login_enabled

# One answer for every credential failure — identical status and body
# for unknown id, no web password and wrong password. The
# body is the FULL sign-in page: the error announced in a
# role=alert element, the form still there to retry from, nothing
# user-derived echoed back — the byte-identical contract holds.
_GENERIC_FAILURE_TEXT = "Invalid credentials."
_GENERIC_LIMIT_TEXT = "Too many login attempts. Try again later."
_INVALID_UID_TEXT = "Invalid user ID"


def _login_page_response(
    request: Request, err: str, status: int
) -> HTMLResponse:
    """Render the sign-in page with server-owned err in role=alert.

    Retain the form, status codes and limiter semantics on every failure.
    Never echo user input: generic credential-failure bodies must stay
    byte-identical. The request carries its CSP nonce from the outer
    security middleware, admitting the inline style without unsafe-inline."""
    nonce = getattr(request.state, "csp_nonce", None)
    nonce_attr = f' nonce="{nonce}"' if nonce else ""
    return HTMLResponse(
        _LOGIN_HTML.format(
            err=err,
            app_name=html.escape(branding.brand_name().upper(), quote=True),
            style_nonce_attr=nonce_attr,
            script_nonce_attr=nonce_attr,
            guest_login=_GUEST_LOGIN_HTML if guest_login_enabled() else "",
            **_privacy_notice_slots(),
        ),
        status_code=status,
    )


_GUEST_LOGIN_HTML = """  <div class="or">or</div>
  <button type="submit" class="guest-btn"
          formaction="/login/guest" formmethod="post" formnovalidate>
    Continue as guest
  </button>
"""

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
{guest_login}{privacy_link}</form>
<script{script_nonce_attr}>
  // The start of the sign-in journey (issue #436). Success is a bare 303
  // to '/', so by the time the signed-in page runs, this document is
  // gone and its performance.now() with it -- the instant has to travel,
  // and it travels with the time origin it is relative to, because the
  // next document's performance.now() starts near zero again.
  // src/perf.js reads both keys back and consumes them; a re-rendered
  // page after a failed attempt simply stamps again.
  document.querySelector('form').addEventListener('submit', function () {{
    try {{
      sessionStorage.setItem('claudit.signin.start', String(performance.now()));
      sessionStorage.setItem('claudit.signin.origin', String(performance.timeOrigin));
    }} catch (e) {{
      // Storage disabled (private browsing, blocked cookies): the
      // journey is simply not measured. A telemetry marker must never be
      // what stops a sign-in.
    }}
  }});
</script>
</body></html>
"""


def _privacy_notice_slots() -> dict[str, str]:
    """Privacy-notice slots, empty unless CLAUDIT_APP_PRIVACY_NOTICE_URL is set.

    An unset notice emits no anchor, href or styling. branding.url_attr
    (SV-BRAND-ESCAPE) drops and logs URLs outside its allow-list: attribute
    escaping alone cannot block a javascript: scheme."""
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
