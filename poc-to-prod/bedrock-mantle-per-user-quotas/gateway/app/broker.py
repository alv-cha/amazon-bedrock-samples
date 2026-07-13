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

Because RoleSessionName is limited to 64 chars of [\\w+=,.@-], we sanitize
the `sub` into a valid session name but keep the *full* `sub` as both the
DynamoDB key and the SourceIdentity, so two users can never collapse onto
one session name without also colliding on SourceIdentity (which we assert).
"""

import hashlib
import re
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from .auth import Identity
from .config import settings

_SESSION_SAFE = re.compile(r"[^\w+=,.@-]")
_MAX_SESSION_NAME = 64


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
    """Map an arbitrary JWT `sub` to a valid, collision-resistant
    RoleSessionName.

    Session names must match [\\w+=,.@-]{1,64}. IdP subs can exceed this
    (e.g. "auth0|5f...e9", long GUIDs). We keep a readable prefix and append
    a short hash of the *full* sub so distinct subs never map to the same
    session name even after sanitization/truncation.
    """
    cleaned = _SESSION_SAFE.sub("-", sub).strip("-") or "user"
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:10]
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
                # SourceIdentity keeps the FULL sub; it is tamper-resistant
                # and propagates to CloudTrail for every downstream call.
                SourceIdentity=sub[:_MAX_SESSION_NAME],
                DurationSeconds=self._ttl,
                # Tag the session so cost-allocation / log queries can also
                # filter by the app user without parsing the ARN.
                Tags=[{"Key": "quota-user", "Value": sub[:256]}],
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
