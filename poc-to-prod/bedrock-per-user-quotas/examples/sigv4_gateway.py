"""SigV4 clients for an IAM-authenticated Lambda Function URL.

SigV4 owns the HTTP Authorization header. Application credentials therefore
travel in dedicated headers:

- X-Quota-User-Token: verified end-user JWT
- X-Quota-Admin-Key: Secrets Manager-backed admin key
"""

import os

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def _credentials(session: boto3.Session | None = None):
    credentials = (session or boto3.Session()).get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials are required to invoke the gateway")
    return credentials.get_frozen_credentials()


def _sign(method: str, url: str, headers: dict[str, str], body,
          region: str, credentials) -> dict[str, str]:
    aws_request = AWSRequest(method=method, url=url, headers=headers, data=body)
    SigV4Auth(credentials, "lambda", region).add_auth(aws_request)
    return dict(aws_request.headers)


class FunctionUrlSigV4Auth(httpx.Auth):
    """httpx auth adapter suitable for OpenAI and Anthropic Python clients."""

    requires_request_body = True

    def __init__(self, user_token: str, region: str | None = None,
                 credentials=None):
        self._user_token = user_token
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._credentials = credentials

    def auth_flow(self, request: httpx.Request):
        request.headers["X-Quota-User-Token"] = self._user_token
        # SDK API-key authentication may have installed a Bearer header. SigV4
        # replaces it; the user token is preserved in the dedicated header.
        request.headers.pop("Authorization", None)
        signed = _sign(
            request.method,
            str(request.url),
            dict(request.headers),
            request.content,
            self._region,
            self._credentials or _credentials(),
        )
        request.headers.update(signed)
        yield request


def signed_request(method: str, url: str, *, region: str | None = None,
                   user_token: str | None = None,
                   admin_key: str | None = None,
                   aws_session: boto3.Session | None = None,
                   http_client: httpx.Client | None = None,
                   **kwargs) -> httpx.Response:
    """Send one SigV4-signed gateway request with optional app credentials."""
    headers = dict(kwargs.pop("headers", {}) or {})
    if user_token:
        headers["X-Quota-User-Token"] = user_token
    if admin_key:
        headers["X-Quota-Admin-Key"] = admin_key

    request = httpx.Request(method, url, headers=headers, **kwargs)
    request.headers.pop("Authorization", None)
    request.headers.update(_sign(
        request.method,
        str(request.url),
        dict(request.headers),
        request.content,
        region or os.environ.get("AWS_REGION", "us-east-1"),
        _credentials(aws_session),
    ))
    if http_client is not None:
        return http_client.send(request)
    with httpx.Client() as client:
        return client.send(request)
