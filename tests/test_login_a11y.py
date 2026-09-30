"""Sign-in page accessibility and failure rendering (issue #395).

Findings 1-3 of the axe-core audit:
1. the Username/Password <label> elements name no input (no for/id, no
   wrapping), so both fields have an empty accessible name;
2. the "or" separator paints #556 over the form card #14181d — 2.44:1,
   below the 4.5:1 AA text floor;
3. every credential failure (401) and the rate-limit answer (429) reply
   with a bare text/plain body — no form, no link back, no title, no
   lang. They must re-render the full sign-in page with the error in a
   role="alert" element, and the generic failure body must stay
   byte-identical between wrong-password and unknown-id (no id
   enumeration, issue #109).
"""
import re
import secrets
from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import auth
from backend import login as login_mod
from backend import session as session_mod

_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _reset_login_state():
    login_mod.reset_login_rate_limits()
    session_mod._USER_CONFIG_CACHE.clear()  # pylint: disable=protected-access
    yield
    login_mod.reset_login_rate_limits()
    session_mod._USER_CONFIG_CACHE.clear()  # pylint: disable=protected-access


@pytest.fixture(name="app")
def _app_fixture():
    a = FastAPI()
    a.middleware("http")(session_mod.auth_middleware)
    a.include_router(login_mod.router)

    @a.get("/api/me")
    def me(request: Request):
        return {"user_id": request.state.user_id}

    return a


@pytest.fixture(name="fake_user")
def _fake_user_fixture(monkeypatch):
    config: dict = {}
    auth.set_web_password(config, "hunter2")
    store = {12345: config}

    def _load(user_id):
        return store.get(user_id)

    monkeypatch.setattr(session_mod, "load_user_config", _load)
    return store


@pytest.fixture(name="fake_session_store")
def _fake_session_store_fixture(monkeypatch):
    rows: dict[int, tuple[str, int, str | None]] = {}

    def get_or_create(user_id, cred_fp):
        row = rows.get(user_id)
        fresh = secrets.token_urlsafe(32)
        if row is None:
            secret, generation = fresh, 0
        elif row[2] != cred_fp:
            secret, generation = fresh, row[1] + 1
        else:
            secret, generation = row[:2]
        rows[user_id] = (secret, generation, cred_fp)
        return secret, generation

    monkeypatch.setattr(
        session_mod, "get_or_create_session_row", get_or_create)
    monkeypatch.setattr(session_mod, "load_session_row", rows.get)
    monkeypatch.setattr(
        session_mod, "bump_session_generation", lambda uid: None)
    return rows


# The generic failure body is the FULL sign-in page (#395): 401,
# text/html, the error announced in the role=alert element. Every
# credential failure answers the same status and the same bytes (#109),
# so the older suites assert through this one helper.
_GENERIC_401 = "Invalid credentials."


def _assert_generic_failure(response):
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert (f'<div class="err" role="alert">{_GENERIC_401}</div>'
            in response.text), (
        "a credential failure did not answer the generic sign-in page "
        "with its message announced")


def _post_login(client, user_id, password):
    return client.post(
        "/login",
        data={"user_id": str(user_id), "password": password},
        headers=_ORIGIN,
        follow_redirects=False,
    )


# -- Finding 1: labels are associated with their inputs ----------------


class _FormScan(HTMLParser):
    """Labels (text + for) and inputs (id + name) of one document."""

    def __init__(self):
        super().__init__()
        self.labels: list[dict] = []
        self.inputs: list[dict] = []
        self._label = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self._label = {"for": a.get("for"), "text": ""}
        elif tag == "input":
            self.inputs.append(
                {"id": a.get("id"), "name": a.get("name"),
                 "in_label": self._label is not None})

    def handle_data(self, data):
        if self._label is not None:
            self._label["text"] += data

    def handle_endtag(self, tag):
        if tag == "label" and self._label is not None:
            self.labels.append(self._label)
            self._label = None


def _scan(html: str) -> _FormScan:
    scan = _FormScan()
    scan.feed(html)
    return scan


def test_signin_labels_are_bound_to_their_inputs(app):
    """Every visible form label names its input through for/id, so both
    fields carry an accessible name (axe `label`)."""
    scan = _scan(TestClient(app).get("/login").text)
    by_id = {i["id"] for i in scan.inputs}
    for want in ("Username", "Password"):
        label = next(
            (lab for lab in scan.labels if want in lab["text"]), None)
        assert label is not None, f"no {want!r} label on the sign-in page"
        assert label["for"], (
            f"the {want!r} label names no input (no for attribute) — the "
            f"field has an empty accessible name")
        assert label["for"] in by_id, (
            f"the {want!r} label's for={label['for']!r} matches no input "
            f"id — the association is broken")


def test_label_association_survives_onto_failed_signins(app, fake_user):
    """The failure page re-renders the same form, so its labels must be
    bound too (the fix for finding 3 must not regress finding 1)."""
    scan = _scan(_post_login(TestClient(app), 12345, "wrong").text)
    for want in ("Username", "Password"):
        label = next(
            (lab for lab in scan.labels if want in lab["text"]), None)
        assert label is not None and label["for"], (
            f"the {want!r} label on the failure page names no input")


# -- Finding 2: the "or" separator meets AA ----------------------------


def _rel_lum(hex_color: str) -> float:
    """WCAG 2.x relative luminance of #rgb or #rrggbb."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    vals = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        vals.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _ratio(fg: str, bg: str) -> float:
    hi, lo = sorted((_rel_lum(fg), _rel_lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_or_separator_meets_aa_against_its_rendered_background(app):
    """The separator paints over the form card, so its color must
    compute >= 4.5:1 against the card's background (axe 2.44:1)."""
    html = TestClient(app).get("/login").text
    or_rule = re.search(r"\.or\s*\{([^}]*)\}", html)
    assert or_rule, "the sign-in page lost its .or separator rule"
    color = re.search(r"color:\s*(#[0-9a-fA-F]{3,6})", or_rule.group(1))
    assert color, ".or sets no color; it inherits whatever the body paints"
    form_rule = re.search(r"form\s*\{([^}]*)\}", html)
    assert form_rule, "the sign-in page lost its form rule"
    bg = re.search(r"background:\s*(#[0-9a-fA-F]{3,6})", form_rule.group(1))
    assert bg, "the form sets no background; the surface moved"
    r = _ratio(color.group(1), bg.group(1))
    assert r >= 4.5, (
        f".or {color.group(1)} over the form card {bg.group(1)} computes "
        f"{r:.2f}:1, below the 4.5:1 AA text floor")


# -- Finding 3: failures re-render the full sign-in page ----------------


def _alert_text(html: str) -> str:
    m = re.search(r'<div class="err" role="alert">(.*?)</div>', html, re.S)
    return m.group(1).strip() if m else ""


def test_failed_signin_renders_the_full_page_with_a_live_error(
    app, fake_user, fake_session_store
):
    """A wrong password answers 401 text/html: the full sign-in page —
    lang, title, bound form, guest route — with the error in a
    role="alert" element, so the failure is announced and the user can
    simply retry."""
    r = _post_login(TestClient(app), 12345, "wrong")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/html"), (
        f"the 401 body is {r.headers['content-type']!r} — a bare "
        f"text/plain error page with no form to retry from")
    assert '<html lang="en">' in r.text
    assert "<title>" in r.text
    assert "<form" in r.text, "the failure page carries no form to retry"
    assert "/login/guest" in r.text, "the failure page lost the guest route"
    alert = _alert_text(r.text)
    assert alert == "Invalid credentials.", (
        f"the role=alert element carries {alert!r}, not the generic "
        f"failure message")


def test_unknown_id_renders_the_same_generic_page(
    app, fake_user, fake_session_store
):
    """The unknown-id 401 answers the same generic page a wrong password
    answers — same status, same body bytes (#109)."""
    client = TestClient(app)
    wrong = _post_login(client, 12345, "wrong")
    unknown = _post_login(client, 999, "anything")
    assert unknown.status_code == 401
    assert unknown.headers["content-type"].startswith("text/html")
    assert unknown.content == wrong.content, (
        "the wrong-password and unknown-id 401 bodies differ — the "
        "difference enumerates account ids (#109)")


def test_rate_limited_signin_renders_the_page_with_a_live_error(
    app, fake_user, fake_session_store
):
    """The 429 keeps its status and limiter semantics and re-renders the
    sign-in page with the rate-limit message announced, so a locked-out
    user can see why and wait — not a bare text/plain line."""
    client = TestClient(app)
    for _ in range(5):
        assert _post_login(client, 12345, "x").status_code == 401
    r = _post_login(client, 12345, "x")
    assert r.status_code == 429
    assert r.headers["content-type"].startswith("text/html"), (
        f"the 429 body is {r.headers['content-type']!r} — bare text")
    assert "<form" in r.text and '<html lang="en">' in r.text
    alert = _alert_text(r.text)
    assert alert == "Too many login attempts. Try again later.", (
        f"the 429 role=alert element carries {alert!r}")


def test_rate_limited_429_body_stays_generic_across_ids(
    app, fake_user, fake_session_store
):
    """Both locked-out shapes answer the same 429 page — a per-id body
    would enumerate which ids exist behind the lock."""
    client = TestClient(app)
    for _ in range(5):
        _post_login(client, 12345, "x")
    locked_pair = _post_login(client, 12345, "x")
    for _ in range(5):
        _post_login(client, 999, "x")
    locked_unknown = _post_login(client, 999, "x")
    assert locked_pair.status_code == locked_unknown.status_code == 429
    assert locked_pair.content == locked_unknown.content


def test_successful_login_still_redirects(app, fake_user, fake_session_store):
    """The failure-page rework must not disturb the success path."""
    r = _post_login(TestClient(app), 12345, "hunter2")
    assert r.status_code in (302, 303)
    assert session_mod.SESSION_COOKIE_NAME in r.cookies
