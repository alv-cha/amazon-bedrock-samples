"""Credential broker: turn a verified end-user JWT into short-lived,
per-user AWS credentials scoped to Bedrock.

This is the heart of the per-user quota model. We do NOT proxy inference
traffic; instead the user authenticates once with the JWT their app
already has, and — if they are within budget — we hand back temporary AWS
credentials they use to call Bedrock *natively* (any API, any provider,
streaming included). Enforcement and attribution both hinge on the JWT
identity:

  JWT `sub`  ->  DynamoDB budget row      (who is billed / blocked)
             ->  RoleSessionName          (shows up in model-invocation
                                            logs -> per-user metering)
             ->  SourceIdentity           (tamper-resistant; can't be
                                            changed on re-assume, so a user
                                            can't relabel as someone else)

RoleSessionName, SourceIdentity, and session-tag values each restrict which
characters they accept, and STS rejects the whole AssumeRole call if any of
them is out of range — so a raw IdP `sub` like "auth0|5f...e9" (pipe) or a
non-ASCII subject breaks vending entirely. We therefore sanitize the `sub`
into ONE collision-resistant identity (``session_name_for``) whose character
set is the intersection valid for all three, and use it for all three. A
short SHA-256 suffix of the *full* sub guarantees two distinct subs never
collapse onto one identity even after sanitization/truncation. The full,
unmodified `sub` is preserved as the DynamoDB key and in the ``SESSION#``
reverse-map row, so metering still attributes usage to the real user.
"""

import hashlib
import re
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from .auth import Identity
from .config import settings

# The vended identity is used as RoleSessionName, SourceIdentity, AND a
# session-tag VALUE. Their allowed charsets differ:
#   RoleSessionName / SourceIdentity: [\w+=,.@-]
#   session-tag value:                [\p{L}\p{Z}\p{N}_.:/=+\-@]  (NO comma)
# We keep only the INTERSECTION so one sanitized value is valid in all three;
# notably comma is dropped (valid in a session name but rejected in a tag
# value, so a sub like an LDAP DN "CN=a,OU=b" would otherwise fail vending).
# re.ASCII so a non-ASCII `sub` (e.g. "josé", CJK) can't leave Unicode word
# characters that STS rejects.
_SESSION_SAFE = re.compile(r"[^\w+=.@-]", re.ASCII)
_MAX_SESSION_NAME = 64
# Hex chars of SHA-256(sub) appended to keep distinct subs from colliding onto
# one identity. 20 hex = 80 bits: birthday-safe past ~10^12 distinct subjects
# (10 hex / 40 bits collided near ~10^6, too weak for large multi-tenant use).
_HASH_HEX = 20


@dataclass(frozen=True)
class VendedCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str  # ISO8601
    user_id: str
    session_name: str


class BrokerError(Exception):
    """Vend refused; .status is the HTTP code, .reason is caller-safe."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def session_name_for(sub: str) -> str:
    """Map an arbitrary JWT `sub` to a single sanitized, collision-resistant
    identity valid as a RoleSessionName, SourceIdentity, AND session-tag value.

    We keep only characters in the intersection of all three fields' charsets
    (ASCII ``[\\w+=.@-]`` — see ``_SESSION_SAFE``; comma excluded because tag
    values reject it) and cap length at 64, since STS rejects the entire
    AssumeRole call if any field is out of range. IdP subs routinely violate
    this (e.g. "auth0|5f...e9", "google-oauth2|123", long GUIDs, non-ASCII).
    We strip disallowed characters to a readable prefix and append an
    ``_HASH_HEX``-char hash of the *full* sub so distinct subs don't collide
    even after sanitization/truncation. The full sub is preserved separately
    (DynamoDB key + SESSION# reverse map), so attribution is unaffected.
    """
    cleaned = _SESSION_SAFE.sub("-", sub).strip("-") or "user"
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:_HASH_HEX]
    suffix = "-" + digest
    prefix = cleaned[: _MAX_SESSION_NAME - len(suffix)]
    return prefix + suffix


class CredentialBroker:
    def __init__(self, sts_client=None, role_arn: str | None = None,
                 ttl_seconds: int | None = None):
        self._sts = sts_client or boto3.client("sts")
        self._role_arn = role_arn if role_arn is not None else settings.bedrock_user_role_arn
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.vended_credential_ttl_seconds

    def vend(self, identity: Identity) -> VendedCredentials:
        """Assume the Bedrock role on behalf of a within-budget user.

        Callers must have already: (1) verified the JWT, (2) confirmed via
        the quota layer that identity.user_id is neither blocked nor over
        budget. This method only performs the STS assumption + identity
        stamping; the budget gate lives in the request handler so a single
        DynamoDB read is shared with logging.
        """
        sub = identity.user_id
        if not sub:
            raise BrokerError(401, "token has no user identity claim")

        session_name = session_name_for(sub)
        if not self._role_arn:
            raise BrokerError(500, "broker role not configured (BEDROCK_USER_ROLE_ARN)")
        try:
            resp = self._sts.assume_role(
                RoleArn=self._role_arn,
                RoleSessionName=session_name,
                # SourceIdentity and the session tag share RoleSessionName's
                # charset limits, so we stamp the SAME sanitized identity in
                # all three. Using the raw sub here would make STS reject the
                # call for any sub containing '|', ':', or non-ASCII (Auth0,
                # Google, Entra, ...). SourceIdentity is still tamper-resistant
                # (can't be changed on re-assume) and propagates to CloudTrail;
                # the full sub is recoverable via the SESSION# reverse map.
                SourceIdentity=session_name,
                DurationSeconds=self._ttl,
                # Tag the session so cost-allocation / log queries can also
                # filter by the app user without parsing the ARN.
                Tags=[{"Key": "quota-user", "Value": session_name}],
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "STSError")
            # Trust-policy misconfig (can't set source identity / tags) is an
            # operator error, surface it distinctly from a plain deny.
            raise BrokerError(500, f"could not vend credentials ({code})")

        creds = resp["Credentials"]
        return VendedCredentials(
            access_key_id=creds["AccessKeyId"],
            secret_access_key=creds["SecretAccessKey"],
            session_token=creds["SessionToken"],
            expiration=creds["Expiration"].isoformat(),
            user_id=sub,
            session_name=session_name,
        )
