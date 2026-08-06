"""Account self-service — the session list and revoke endpoints.

Deliberately NOT under /api/sessions: that prefix already serves Claude Code
transcript data and is guest-blocked by the middleware. The guest/identity
checks below live in the handlers (not the middleware's guest-deny list), so
they hold even if this router is ever mounted without auth_middleware.

Connection budget (plan Amendment 1): each handler acquires at most one
auth-DB connection, via a single sessions_repo call, and never holds it
across an await.
"""
from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend import sessions_repo

router = APIRouter(prefix="/account")


def _guest_block(request: Request) -> JSONResponse | None:
    if getattr(request.state, "is_guest", False):
        return JSONResponse(
            {"ok": False, "error": "Forbidden (guest)"}, status_code=403
        )
    return None


def _user_id_or_401(request: Request) -> int | JSONResponse:
    """The caller's user_id, or the 401 to return instead.

    A cookie-less request (or one mounted without the middleware) leaves no
    user_id on request.state, and the guest sentinel 0 is falsy — neither
    may reach sessions_repo.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return JSONResponse(
            {"ok": False, "error": "Unauthorized"}, status_code=401
        )
    return user_id


@router.get("/sessions")
def list_account_sessions(request: Request):
    blocked = _guest_block(request)
    if blocked is not None:
        return blocked
    user_id = _user_id_or_401(request)
    if isinstance(user_id, JSONResponse):
        return user_id
    return {"ok": True, "sessions": sessions_repo.list_sessions(user_id)}


@router.post("/sessions/revoke")
async def revoke_account_session(request: Request):
    blocked = _guest_block(request)
    if blocked is not None:
        return blocked
    user_id = _user_id_or_401(request)
    if isinstance(user_id, JSONResponse):
        return user_id
    try:
        body = await request.json()
    except ValueError:
        body = None
    nonce = (
        str(body.get("nonce", "")).strip() if isinstance(body, dict) else ""
    )
    if not nonce:
        return JSONResponse(
            {"ok": False, "error": "nonce required"}, status_code=400
        )
    return {"ok": True, "revoked": sessions_repo.revoke_session(user_id, nonce)}
