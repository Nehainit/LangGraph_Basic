"""Clerk sign-in: the API accepts only valid Clerk session tokens once a publishable key is configured."""

import base64
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from video_automation import auth
from video_automation.backend import app


FRONTEND_API = "example-app.clerk.accounts.dev"
PUBLISHABLE_KEY = "pk_test_" + base64.b64encode(f"{FRONTEND_API}$".encode()).decode().rstrip("=")
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwksClient:
    """Stands in for Clerk's JWKS endpoint, so tests never touch the network."""

    def get_signing_key_from_jwt(self, token):
        return type("SigningKey", (), {"key": PRIVATE_KEY.public_key()})()


def make_token(private_key=PRIVATE_KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "user_123",
        "iss": f"https://{FRONTEND_API}",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "azp": "http://127.0.0.1:8001",
        **overrides,
    }
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, private_key, algorithm="RS256")


@pytest.fixture
def signed_in_app(monkeypatch):
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", PUBLISHABLE_KEY)
    monkeypatch.delenv("CLERK_AUTHORIZED_PARTIES", raising=False)
    monkeypatch.setattr(auth, "_jwks_client", lambda url: FakeJwksClient())
    return TestClient(app)


def test_frontend_api_is_decoded_from_the_publishable_key():
    assert auth.frontend_api(PUBLISHABLE_KEY) == FRONTEND_API


@pytest.mark.parametrize("key", ["sk_test_abc", "pk_test_", "pk_test_!!!notbase64", "pk_test_" + base64.b64encode(b"nohost").decode()])
def test_malformed_publishable_keys_are_rejected(key):
    with pytest.raises(ValueError):
        auth.frontend_api(key)


def test_sign_in_is_off_without_a_publishable_key():
    client = TestClient(app)
    assert client.get("/api/auth-config").json() == {"enabled": False}
    assert client.get("/api/model-catalog").status_code == 200


def test_auth_config_exposes_only_public_values(signed_in_app):
    assert signed_in_app.get("/api/auth-config").json() == {
        "enabled": True,
        "publishable_key": PUBLISHABLE_KEY,
        "frontend_api": FRONTEND_API,
    }


def test_auth_pages_are_served(signed_in_app):
    for path in ("/sign-in", "/sign-up", "/static/auth.js", "/static/auth-page.js", "/static/auth.css"):
        assert signed_in_app.get(path).status_code == 200, path


def test_protected_routes_require_a_session(signed_in_app):
    assert signed_in_app.get("/api/model-catalog").status_code == 401
    assert signed_in_app.post("/api/create-video", json={"topic": "A fable"}).status_code == 401
    assert signed_in_app.post("/api/review-visual-storyboard", json={"thread_id": "x", "action": "approve"}).status_code == 401
    assert signed_in_app.get("/output/outputs/anything.png").status_code == 401


def test_a_valid_bearer_token_is_accepted(signed_in_app):
    response = signed_in_app.get("/api/model-catalog", headers={"Authorization": f"Bearer {make_token()}"})
    assert response.status_code == 200


def test_the_session_cookie_is_accepted_for_media_requests(signed_in_app):
    signed_in_app.cookies.set(auth.SESSION_COOKIE, make_token())
    # Authorized, so the request reaches the file lookup and fails there instead of at sign-in.
    assert signed_in_app.get("/output/outputs/missing.png").status_code == 404


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(make_token(exp=int(time.time()) - 120, iat=int(time.time()) - 300, nbf=int(time.time()) - 300), id="expired"),
        pytest.param(make_token(iss="https://someone-else.clerk.accounts.dev"), id="wrong-issuer"),
        pytest.param(make_token(azp="https://evil.example"), id="wrong-origin"),
        pytest.param(make_token(private_key=OTHER_KEY), id="wrong-signature"),
        pytest.param(make_token(sub=None), id="no-subject"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
def test_bad_tokens_are_rejected(signed_in_app, token):
    response = signed_in_app.get("/api/model-catalog", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_authorized_parties_can_be_configured(signed_in_app, monkeypatch):
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://studio.example.com/")
    headers = {"Authorization": f"Bearer {make_token(azp='https://studio.example.com')}"}
    assert signed_in_app.get("/api/model-catalog", headers=headers).status_code == 200
    local = {"Authorization": f"Bearer {make_token()}"}
    assert signed_in_app.get("/api/model-catalog", headers=local).status_code == 401


def test_a_broken_key_locks_the_api_instead_of_disabling_sign_in(monkeypatch):
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_broken")
    client = TestClient(app)
    assert client.get("/api/model-catalog").status_code == 503
    assert client.get("/api/auth-config").json()["enabled"] is False
