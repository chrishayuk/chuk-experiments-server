"""Google sign-in for the dashboard, restricted to one email address.

Not the same thing as auth.py's bearer-token API auth (that's for REST/MCP
clients, which can set an Authorization header) — this is a browser session:
a human visits a page, signs in with Google, and gets an HttpOnly cookie
good for a week. Stateless — no session table — the cookie is an
HMAC-signed "email:expiry" payload, verified on every request rather than
looked up.
"""

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

import httpx
from starlette.requests import Request

from .config import settings
from .constants import (
    GOOGLE_AUTH_URL,
    GOOGLE_OAUTH_SCOPE,
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
)


class GoogleAuthError(Exception):
    pass


def _sign(payload: str) -> str:
    signature = hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify(token: str) -> str | None:
    payload, sep, signature = token.rpartition(".")
    if not sep:
        return None
    expected = hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    email, sep, expiry = payload.rpartition(":")
    if not sep or not expiry.isdigit() or int(expiry) < time.time():
        return None
    return email


def create_session_cookie_value(email: str) -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE_SECONDS
    return _sign(f"{email}:{expiry}")


def get_authenticated_email(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        return _verify(token)
    except RuntimeError:
        # SESSION_SECRET isn't configured (e.g. local dev, where dashboard
        # auth isn't set up at all) — no way a valid session cookie could
        # have been minted without it, so a cookie showing up here is
        # stale/foreign, not a crash-worthy condition.
        return None


def is_authenticated(request: Request) -> bool:
    email = get_authenticated_email(request)
    return email is not None and email == settings.dashboard_allowed_email


def new_oauth_state() -> str:
    return secrets.token_urlsafe(24)


def build_google_auth_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPE,
        "state": state,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_email(code: str) -> str:
    """Exchange an OAuth authorization code for the signed-in user's
    verified email. Raises GoogleAuthError on any failure — callers should
    treat this as "sign-in didn't work", not surface Google's error detail."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            raise GoogleAuthError(f"token exchange failed: {token_resp.status_code}")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise GoogleAuthError("token exchange response had no access_token")

        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_resp.status_code >= 400:
            raise GoogleAuthError(f"userinfo request failed: {userinfo_resp.status_code}")
        userinfo = userinfo_resp.json()

    if not userinfo.get("email_verified"):
        raise GoogleAuthError("Google account email is not verified")
    email = userinfo.get("email")
    if not email:
        raise GoogleAuthError("userinfo response had no email")
    return email
