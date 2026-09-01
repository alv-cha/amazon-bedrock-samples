"""SigV4 clients for the IAM-authenticated broker/admin Function URL.

SigV4 owns the HTTP Authorization header. Application credentials therefore
travel in dedicated headers:

- X-Quota-User-Token: verified end-user JWT
- X-Quota-Admin-Key: Secrets Manager-backed admin key
"""

import argparse
import json
import os
from urllib.parse import quote

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
    """httpx auth adapter for broker and administrative API calls."""

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
                   emergency_key: str | None = None,
                   aws_session: boto3.Session | None = None,
                   http_client: httpx.Client | None = None,
                   timeout=None,
                   **kwargs) -> httpx.Response:
    """Send one SigV4-signed gateway request with optional app credentials."""
    headers = dict(kwargs.pop("headers", {}) or {})
    if user_token:
        headers["X-Quota-User-Token"] = user_token
    if admin_key:
        headers["X-Quota-Admin-Key"] = admin_key
    if emergency_key:
        headers["X-Quota-Emergency-Key"] = emergency_key

    def send(client: httpx.Client) -> httpx.Response:
        request_kwargs = dict(kwargs)
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        request = client.build_request(
            method, url, headers=headers, **request_kwargs
        )
        request.headers.pop("Authorization", None)
        request.headers.update(_sign(
            request.method,
            str(request.url),
            dict(request.headers),
            request.content,
            region or os.environ.get("AWS_REGION", "us-east-1"),
            _credentials(aws_session),
        ))
        return client.send(request)

    if http_client is not None:
        return send(http_client)
    with httpx.Client() as client:
        return send(client)


def _admin_request_args(args) -> tuple[str, str, dict]:
    base = args.gateway_url.rstrip("/")
    if args.command == "create-user":
        return "POST", f"{base}/admin/users", {
            "json": {
                "user_id": args.user_id,
                "name": args.name or args.user_id,
                "daily_usd": args.daily_usd,
                "daily_input_tokens": args.daily_input_tokens,
                "daily_output_tokens": args.daily_output_tokens,
            }
        }
    if args.command == "list-users":
        return "GET", f"{base}/admin/users", {}
    if args.command in {"emergency-stop", "emergency-recover"}:
        activate = args.command == "emergency-stop"
        return "POST", f"{base}/admin/emergency-stop", {
            "json": {
                "action": "activate" if activate else "recover",
                "confirmation": (
                    "STOP_ALL_BEDROCK_SESSIONS"
                    if activate
                    else "RESTORE_ALL_BEDROCK_SESSIONS"
                ),
                "reason": args.reason,
            }
        }

    user_id = quote(args.user_id, safe="")
    if args.command == "update-user":
        limits = {
            name: getattr(args, name)
            for name in (
                "daily_usd",
                "daily_input_tokens",
                "daily_output_tokens",
            )
            if getattr(args, name) is not None
        }
        if not limits:
            raise ValueError(
                "update-user requires at least one daily quota option"
            )
        return "PUT", f"{base}/admin/users/{user_id}/limits", {"json": limits}
    if args.command in {"block-user", "unblock-user"}:
        status = "blocked" if args.command == "block-user" else "active"
        return "PUT", f"{base}/admin/users/{user_id}/status", {
            "json": {"status": status, "reason": args.reason}
        }
    if args.command == "get-usage":
        params = {"window": args.window} if args.window else {}
        return "GET", f"{base}/admin/users/{user_id}/usage", {"params": params}
    raise ValueError(f"Unsupported command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SigV4-signed administrative client for the quota gateway."
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("GATEWAY_URL"),
        help="BrokerApiUrl stack output (or GATEWAY_URL).",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS CLI profile used for SigV4 credentials.",
    )
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("ADMIN_KEY"),
        help="Routine admin API key (or ADMIN_KEY).",
    )
    parser.add_argument(
        "--emergency-key",
        default=os.environ.get("EMERGENCY_ADMIN_KEY"),
        help="Break-glass emergency key (or EMERGENCY_ADMIN_KEY).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user")
    create.add_argument("user_id")
    create.add_argument("--name")
    create.add_argument("--daily-usd", type=float, required=True)
    create.add_argument("--daily-input-tokens", type=int, required=True)
    create.add_argument("--daily-output-tokens", type=int, required=True)

    commands.add_parser("list-users")

    emergency_stop = commands.add_parser("emergency-stop")
    emergency_stop.add_argument("--reason", required=True)

    emergency_recover = commands.add_parser("emergency-recover")
    emergency_recover.add_argument("--reason", required=True)

    update = commands.add_parser("update-user")
    update.add_argument("user_id")
    update.add_argument("--daily-usd", type=float)
    update.add_argument("--daily-input-tokens", type=int)
    update.add_argument("--daily-output-tokens", type=int)

    block = commands.add_parser("block-user")
    block.add_argument("user_id")
    block.add_argument("--reason", default="admin CLI")

    unblock = commands.add_parser("unblock-user")
    unblock.add_argument("user_id")
    unblock.add_argument("--reason", default="admin CLI")

    usage = commands.add_parser("get-usage")
    usage.add_argument("user_id")
    usage.add_argument("--window", help="UTC window in YYYY-MM-DD format.")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if not args.gateway_url:
        parser.error("--gateway-url or GATEWAY_URL is required")
    is_emergency = args.command in {"emergency-stop", "emergency-recover"}
    if is_emergency and not args.emergency_key:
        parser.error(
            "--emergency-key or EMERGENCY_ADMIN_KEY is required for "
            "break-glass commands"
        )
    if not is_emergency and not args.admin_key:
        parser.error("--admin-key or ADMIN_KEY is required")
    try:
        method, url, request_kwargs = _admin_request_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    response = signed_request(
        method,
        url,
        region=args.region,
        admin_key=(None if is_emergency else args.admin_key),
        emergency_key=(args.emergency_key if is_emergency else None),
        aws_session=session,
        **request_kwargs,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"status_code": response.status_code, "body": response.text}
    print(json.dumps(body, indent=2, sort_keys=True))
    response.raise_for_status()


if __name__ == "__main__":
    main()
