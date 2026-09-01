import httpx
from botocore.credentials import Credentials

from examples.sigv4_gateway import (
    FunctionUrlSigV4Auth,
    _admin_request_args,
    _parser,
    signed_request,
)


def test_httpx_auth_preserves_user_token_outside_authorization():
    auth = FunctionUrlSigV4Auth(
        "user-jwt",
        region="us-east-1",
        credentials=Credentials("AKID", "SECRET", "SESSION"),
    )
    request = httpx.Request(
        "POST",
        "https://example.lambda-url.us-east-1.on.aws/v1/credentials",
        headers={"Authorization": "Bearer sdk-api-key"},
        content=b"",
    )

    signed = next(auth.auth_flow(request))

    assert signed.headers["X-Quota-User-Token"] == "user-jwt"
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert signed.headers["X-Amz-Security-Token"] == "SESSION"


def test_signed_admin_request_keeps_admin_key_outside_authorization():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = signed_request(
            "POST",
            "https://example.lambda-url.us-east-1.on.aws/admin/users",
            region="us-east-1",
            admin_key="admin-secret",
            aws_session=AwsSession(),
            http_client=client,
            timeout=17,
            json={"user_id": "alice"},
        )

    assert response.status_code == 200
    request = captured["request"]
    assert request.headers["X-Quota-Admin-Key"] == "admin-secret"
    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert request.extensions["timeout"] == {
        "connect": 17,
        "read": 17,
        "write": 17,
        "pool": 17,
    }


def test_signed_emergency_request_uses_separate_break_glass_header():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(202, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        signed_request(
            "POST",
            "https://example.lambda-url.us-east-1.on.aws/admin/emergency-stop",
            region="us-east-1",
            emergency_key="break-glass-secret",
            aws_session=AwsSession(),
            http_client=client,
            json={"action": "activate"},
        )

    request = captured["request"]
    assert request.headers["X-Quota-Emergency-Key"] == "break-glass-secret"
    assert "X-Quota-Admin-Key" not in request.headers


def test_admin_cli_create_and_update_include_all_quota_dimensions():
    create = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "create-user", "tenant/acme",
        "--daily-usd", "25",
        "--daily-input-tokens", "1000000",
        "--daily-output-tokens", "200000",
    ])
    method, url, kwargs = _admin_request_args(create)
    assert (method, url) == ("POST", "https://example.test/admin/users")
    assert kwargs["json"] == {
        "user_id": "tenant/acme",
        "name": "tenant/acme",
        "daily_usd": 25.0,
        "daily_input_tokens": 1_000_000,
        "daily_output_tokens": 200_000,
    }

    update = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "update-user", "tenant/acme",
        "--daily-usd", "30",
        "--daily-input-tokens", "2000000",
        "--daily-output-tokens", "400000",
    ])
    method, url, kwargs = _admin_request_args(update)
    assert method == "PUT"
    assert url == "https://example.test/admin/users/tenant%2Facme/limits"
    assert kwargs["json"] == {
        "daily_usd": 30.0,
        "daily_input_tokens": 2_000_000,
        "daily_output_tokens": 400_000,
    }


def test_admin_cli_emergency_commands_include_explicit_confirmation():
    stop = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "emergency-stop", "--reason", "security incident",
    ])
    method, url, kwargs = _admin_request_args(stop)
    assert (method, url) == (
        "POST",
        "https://example.test/admin/emergency-stop",
    )
    assert kwargs["json"] == {
        "action": "activate",
        "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
        "reason": "security incident",
    }

    recover = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "emergency-recover", "--reason", "incident resolved",
    ])
    _, _, kwargs = _admin_request_args(recover)
    assert kwargs["json"]["action"] == "recover"
    assert kwargs["json"]["confirmation"] == (
        "RESTORE_ALL_BEDROCK_SESSIONS"
    )
