"""Clerk sign-in for the studio: the browser signs in with Clerk, the API checks Clerk's session token."""

import base64
import binascii
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request


logger = logging.getLogger(__name__)
DEFAULT_AUTHORIZED_PARTIES = ("http://127.0.0.1:8001", "http://localhost:8001")
SESSION_COOKIE = "__session"


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
        return None
    token = _request_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    try:
        claims = verify_session_token(token, settings)
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        logger.info("Rejected session token: %s", exc)
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.") from exc
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
        return {"enabled": False}
    return {"enabled": True, "publishable_key": settings.publishable_key, "frontend_api": settings.frontend_api}
