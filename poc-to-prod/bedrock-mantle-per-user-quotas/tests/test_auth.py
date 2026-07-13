"""JWT verification tests: HS256 dev mode and the RS256/JWKS path."""

import time

import jwt as pyjwt
import pytest

import app.auth as auth_module
from app.auth import JwtError, JwtVerifier
from app.config import Settings

SECRET = "test-jwt-secret"  # matches conftest JWT_SHARED_SECRET


def make_jwt(sub="alice", secret=SECRET, exp_in=3600, extra=None, algorithm="HS256", key=None):
    claims = {"sub": sub, "exp": int(time.time()) + exp_in, **(extra or {})}
    if claims.get("sub") is None:
        del claims["sub"]
    return pyjwt.encode(claims, key or secret, algorithm=algorithm)


def test_valid_token_yields_identity():
    identity = JwtVerifier().verify(make_jwt("user-42", extra={"email": "u@example.com"}))
    assert identity.user_id == "user-42"
    assert identity.claims["email"] == "u@example.com"


def test_expired_token_rejected():
    with pytest.raises(JwtError, match="expired"):
        JwtVerifier().verify(make_jwt(exp_in=-10))


def test_wrong_secret_rejected():
    with pytest.raises(JwtError, match="invalid token"):
        JwtVerifier().verify(make_jwt(secret="other-secret"))


def test_missing_sub_claim_rejected():
    with pytest.raises(JwtError, match="'sub' claim"):
        JwtVerifier().verify(make_jwt(sub=None))


def test_token_without_exp_rejected():
    token = pyjwt.encode({"sub": "alice"}, SECRET, algorithm="HS256")
    with pytest.raises(JwtError):
        JwtVerifier().verify(token)


def test_audience_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("JWT_AUDIENCE", "my-client-id")
    monkeypatch.setattr(auth_module, "settings", Settings())

    with pytest.raises(JwtError, match="aud"):
        JwtVerifier().verify(make_jwt())  # no aud claim

    identity = JwtVerifier().verify(make_jwt(extra={"aud": "my-client-id"}))
    assert identity.user_id == "alice"


def test_issuer_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setattr(auth_module, "settings", Settings())

    with pytest.raises(JwtError, match="issuer"):
        JwtVerifier().verify(make_jwt(extra={"iss": "https://evil.example.com"}))

    identity = JwtVerifier().verify(make_jwt(extra={"iss": "https://idp.example.com"}))
    assert identity.user_id == "alice"


def test_custom_user_claim(monkeypatch):
    monkeypatch.setenv("JWT_USER_CLAIM", "cognito:username")
    monkeypatch.setattr(auth_module, "settings", Settings())

    identity = JwtVerifier().verify(make_jwt(extra={"cognito:username": "carol"}))
    assert identity.user_id == "carol"


def test_rs256_via_jwks(monkeypatch):
    """Production path: asymmetric signature resolved through a JWKS client."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = make_jwt("rsa-user", algorithm="RS256", key=private_key,
                     extra={"iss": "https://idp.example.com"})

    # No shared secret -> forces the JWKS path.
    monkeypatch.setenv("JWT_SHARED_SECRET", "")
    monkeypatch.setenv("JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setattr(auth_module, "settings", Settings())

    class FakeSigningKey:
        key = private_key.public_key()

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, tok):
            return FakeSigningKey()

    identity = JwtVerifier(jwks_client=FakeJwksClient()).verify(token)
    assert identity.user_id == "rsa-user"

    # HS256 tokens must NOT be accepted on the JWKS path (alg confusion).
    with pytest.raises(JwtError):
        JwtVerifier(jwks_client=FakeJwksClient()).verify(make_jwt())
