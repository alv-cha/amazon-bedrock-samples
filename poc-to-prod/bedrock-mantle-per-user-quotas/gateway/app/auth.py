"""JWT authentication: the user identity comes from the customer's own IdP.

Instead of gateway-issued API keys, clients send the JWT their application
already uses (Cognito, Okta, Auth0, Entra ID, ... any OIDC issuer). The
gateway verifies it and takes the quota identity from a configurable claim
(default ``sub``).

Two verification modes:

- **JWKS (production)** — RS256/ES256 signatures verified against the
  issuer's published JWKS (``JWT_JWKS_URL``, derived from ``JWT_ISSUER`` if
  not set). Keys are fetched once and cached by PyJWKClient.
- **Shared secret (dev/test)** — set ``JWT_SHARED_SECRET`` to verify HS256
  tokens without an IdP. Never use in production.

``iss`` and ``aud`` are enforced when configured; ``exp`` always is.
"""

from dataclasses import dataclass

import jwt as pyjwt

from .config import settings


class JwtError(Exception):
    """Verification failed; .reason is safe to return to the caller."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Identity:
    user_id: str
    claims: dict


def extract_bearer(authorization_header: str | None) -> str | None:
    """Pull a bearer token out of an Authorization or x-api-key style value."""
    if not authorization_header:
        return None
    value = authorization_header.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


class JwtVerifier:
    def __init__(self, jwks_client: "pyjwt.PyJWKClient | None" = None):
        self._jwks_client = jwks_client

    def _signing_key(self, token: str):
        if self._jwks_client is None:
            jwks_url = settings.jwt_jwks_url
            if not jwks_url and settings.jwt_issuer:
                jwks_url = settings.jwt_issuer.rstrip("/") + "/.well-known/jwks.json"
            if not jwks_url:
                raise JwtError(
                    "gateway is not configured with a JWT issuer/JWKS URL or shared secret"
                )
            self._jwks_client = pyjwt.PyJWKClient(jwks_url, cache_keys=True)
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> Identity:
        options = {"require": ["exp"], "verify_aud": bool(settings.jwt_audience)}
        try:
            if settings.jwt_shared_secret:
                claims = pyjwt.decode(
                    token,
                    settings.jwt_shared_secret,
                    algorithms=["HS256"],
                    audience=settings.jwt_audience or None,
                    issuer=settings.jwt_issuer or None,
                    options=options,
                )
            else:
                claims = pyjwt.decode(
                    token,
                    self._signing_key(token),
                    algorithms=["RS256", "ES256", "RS384", "ES384", "RS512"],
                    audience=settings.jwt_audience or None,
                    issuer=settings.jwt_issuer or None,
                    options=options,
                )
        except pyjwt.ExpiredSignatureError:
            raise JwtError("token has expired")
        except pyjwt.InvalidAudienceError:
            raise JwtError("token audience does not match this gateway")
        except pyjwt.InvalidIssuerError:
            raise JwtError("token issuer does not match this gateway")
        except pyjwt.PyJWTError as e:
            raise JwtError(f"invalid token: {e}")

        user_id = claims.get(settings.jwt_user_claim)
        if not user_id or not isinstance(user_id, str):
            raise JwtError(
                f"token is missing the '{settings.jwt_user_claim}' claim used as the user id"
            )
        return Identity(user_id=user_id, claims=claims)
