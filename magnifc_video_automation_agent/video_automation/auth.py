"""Clerk sign-in for the studio: the browser signs in with Clerk, the API checks Clerk's session token."""

import base64
import binascii
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib import error, request as urlrequest

import jwt
from fastapi import HTTPException, Request


logger = logging.getLogger(__name__)
DEFAULT_AUTHORIZED_PARTIES = ("http://127.0.0.1:8001", "http://localhost:8001")
SESSION_COOKIE = "__session"
CLERK_API = "https://api.clerk.com/v1"
# How long an account's email addresses are remembered before Clerk is asked again.
EMAIL_CACHE_SECONDS = 600
_emails: dict[str, tuple[float, frozenset[str]]] = {}
_emails_lock = threading.Lock()


def sign_in_required() -> bool:
    """REQUIRE_SIGN_IN=true on a deployed server: a missing Clerk key then locks the API instead of opening it."""
    return os.getenv("REQUIRE_SIGN_IN", "").strip().lower() in {"1", "true", "yes"}


def allowed_users() -> frozenset[str]:
    """ALLOWED_USERS: comma-separated Clerk user IDs and/or email addresses; empty lets every signed-in account in."""
    return frozenset(item.strip().lower() for item in os.getenv("ALLOWED_USERS", "").split(",") if item.strip())


def _account_emails(user_id: str, claims: dict) -> frozenset[str]:
    """The account's email addresses: from the session token when it carries them, else from Clerk's API."""
    claimed = claims.get("email") or claims.get("primary_email_address")
    if isinstance(claimed, str) and claimed:
        return frozenset({claimed.lower()})
    secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not secret:
        return frozenset()
    with _emails_lock:
        cached = _emails.get(user_id)
        if cached and time.time() - cached[0] < EMAIL_CACHE_SECONDS:
            return cached[1]
    try:
        call = urlrequest.Request(f"{CLERK_API}/users/{user_id}", headers={"Authorization": f"Bearer {secret}"})
        with urlrequest.urlopen(call, timeout=10) as response:
            account = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, error.URLError) as exc:
        logger.warning("Could not read the email addresses of %s from Clerk: %s", user_id, exc)
        return frozenset()
    emails = frozenset(
        str(item.get("email_address", "")).lower() for item in account.get("email_addresses") or [] if item.get("email_address")
    )
    with _emails_lock:
        _emails[user_id] = (time.time(), emails)
    return emails


def check_invited(user_id: str, claims: dict) -> None:
    """Refuse a signed-in account that is not on the invite list, so strangers cannot spend the studio's credits."""
    allowed = allowed_users()
    if not allowed or user_id.lower() in allowed or _account_emails(user_id, claims) & allowed:
        return
    logger.warning("Refused uninvited account %s", user_id)
    raise HTTPException(status_code=403, detail={
        "error": "This studio is invite-only and your account isn't on the list yet. Ask the owner for access.",
        "code": "not_invited",
    })


@dataclass(frozen=True)
class AuthSettings:
    publishable_key: str
    frontend_api: str
    authorized_parties: tuple[str, ...]

    @property
    def issuer(self) -> str:
        return f"https://{self.frontend_api}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


def frontend_api(publishable_key: str) -> str:
    """Clerk publishable keys are `pk_<env>_` + base64 of the Frontend API host followed by `$`."""
    parts = publishable_key.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != "pk" or not parts[2]:
        raise ValueError("CLERK_PUBLISHABLE_KEY must look like pk_test_… or pk_live_….")
    encoded = parts[2] + "=" * (-len(parts[2]) % 4)
    try:
        host = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("CLERK_PUBLISHABLE_KEY is not a valid Clerk publishable key.") from exc
    if not host.endswith("$") or "." not in host:
        raise ValueError("CLERK_PUBLISHABLE_KEY is not a valid Clerk publishable key.")
    return host[:-1]


def auth_settings() -> AuthSettings | None:
    """Read on every request so tests and a changed .env never see a stale configuration."""
    key = os.getenv("CLERK_PUBLISHABLE_KEY", "").strip()
    if not key:
        return None
    parties = tuple(
        party.strip().rstrip("/")
        for party in os.getenv("CLERK_AUTHORIZED_PARTIES", ",".join(DEFAULT_AUTHORIZED_PARTIES)).split(",")
        if party.strip()
    )
    return AuthSettings(key, frontend_api(key), parties)


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    # PyJWKClient caches Clerk's signing keys, so tokens are verified without a network call each time.
    return jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)


def verify_session_token(token: str, settings: AuthSettings, jwks_client=None) -> dict:
    client = jwks_client or _jwks_client(settings.jwks_url)
    signing_key = client.get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        issuer=settings.issuer,
        leeway=5,
        options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False},
    )
    # azp is the origin that requested the token; it stops a token minted for another site being replayed here.
    authorized_party = claims.get("azp")
    if authorized_party and settings.authorized_parties and authorized_party.rstrip("/") not in settings.authorized_parties:
        raise jwt.InvalidTokenError(f"Token was issued for {authorized_party}, which is not an authorized party.")
    return claims


def _request_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    # Images and videos load through <img>/<video> tags, which cannot send headers but do send Clerk's cookie.
    return request.cookies.get(SESSION_COOKIE) or None


def require_user(request: Request) -> str | None:
    """FastAPI dependency: the signed-in Clerk user ID, or None when sign-in is not configured."""
    try:
        settings = auth_settings()
    except ValueError as exc:
        # A broken key must lock the API, not silently turn sign-in off.
        raise HTTPException(status_code=503, detail="Sign-in is misconfigured on the server.") from exc
    if settings is None:
        if sign_in_required():
            # A deployed server must never fall back to an open API because a key went missing.
            raise HTTPException(status_code=503, detail="Sign-in is required on this server but is not configured.")
        return None
    token = _request_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    try:
        claims = verify_session_token(token, settings)
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        logger.info("Rejected session token: %s", exc)
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.") from exc
    check_invited(claims["sub"], claims)
    request.state.user_id = claims["sub"]
    return claims["sub"]


def public_auth_config() -> dict:
    """What the browser needs to load Clerk. The publishable key is public by design."""
    try:
        settings = auth_settings()
    except ValueError as exc:
        logger.error("Sign-in is misconfigured: %s", exc)
        return {"enabled": False, "error": str(exc)}
    if settings is None:
        return {"enabled": False, **({"error": "Sign-in is required but not configured."} if sign_in_required() else {})}
    return {"enabled": True, "publishable_key": settings.publishable_key, "frontend_api": settings.frontend_api}
