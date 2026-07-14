import httpx
from botocore.credentials import Credentials

from examples.sigv4_gateway import FunctionUrlSigV4Auth, signed_request


def test_httpx_auth_preserves_user_token_outside_authorization():
    auth = FunctionUrlSigV4Auth(
        "user-jwt",
        region="us-east-1",
        credentials=Credentials("AKID", "SECRET", "SESSION"),
    )
    request = httpx.Request(
        "POST",
        "https://example.lambda-url.us-east-1.on.aws/v1/responses",
        headers={"Authorization": "Bearer sdk-api-key"},
        json={"model": "example", "input": "hello"},
    )

    signed = next(auth.auth_flow(request))

    assert signed.headers["X-Quota-User-Token"] == "user-jwt"
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert signed.headers["X-Amz-Security-Token"] == "SESSION"


def test_signed_admin_request_keeps_admin_key_outside_authorization():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    class HttpClient:
        request = None

        def send(self, request):
            self.request = request
            return httpx.Response(200, request=request)

    client = HttpClient()
    response = signed_request(
        "POST",
        "https://example.lambda-url.us-east-1.on.aws/admin/users",
        region="us-east-1",
        admin_key="admin-secret",
        aws_session=AwsSession(),
        http_client=client,
        json={"user_id": "alice"},
    )

    assert response.status_code == 200
    assert client.request.headers["X-Quota-Admin-Key"] == "admin-secret"
    assert client.request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
