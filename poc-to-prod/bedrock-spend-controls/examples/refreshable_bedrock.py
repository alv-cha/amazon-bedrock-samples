"""Lazy, refresh-aware credentials for direct Amazon Bedrock Runtime calls.

The broker remains out of the inference path. Botocore asks this provider for
credentials on the first signed Bedrock request and near the effective lease
expiration. All calls between refreshes reuse the cached credential set.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import boto3
import httpx
from botocore.credentials import DeferredRefreshableCredentials
from botocore.exceptions import CredentialRetrievalError
from botocore.session import get_session

try:
    from .sigv4_gateway import signed_request
except ImportError:  # Direct execution from the examples directory.
    from sigv4_gateway import signed_request


class BrokerCredentialError(CredentialRetrievalError):
    """The broker refused or could not complete a credential refresh."""

    def __init__(
        self,
        message: str,
        *,
        terminal: bool,
        error_type: str = "",
    ):
        super().__init__(provider="bedrock-spend-controls-broker", error_msg=message)
        self.terminal = terminal
        self.error_type = error_type


class QuotaBrokerCredentialProvider:
    """Own one lazy, single-flight botocore credential cache per quota user."""

    def __init__(
        self,
        gateway_url: str,
        user_jwt: str | Callable[[], str],
        *,
        region: str = "us-east-1",
        aws_session: boto3.Session | None = None,
        http_client: httpx.Client | None = None,
        vend_callable: Callable[[str], dict] | None = None,
        time_fetcher: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.gateway_url = gateway_url.rstrip("/")
        self._user_token_supplier = (
            user_jwt if callable(user_jwt) else lambda: user_jwt
        )
        self.region = region
        self.aws_session = aws_session or boto3.Session(region_name=region)
        self.http_client = http_client
        self._vend_callable = vend_callable
        self._time = time_fetcher or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self._max_attempts = max_attempts
        self._next_lease_id = str(uuid.uuid4())
        self._state_lock = threading.Lock()
        self._credentials_holder: dict[str, DeferredRefreshableCredentials] = {}
        credentials = DeferredRefreshableCredentials(
            refresh_using=self._refresh_metadata,
            method="bedrock-spend-controls-broker",
            time_fetcher=self._time,
        )
        self._credentials_holder["credentials"] = credentials
        self.credentials = credentials

    @staticmethod
    def _parse_time(value: str, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise BrokerCredentialError(
                f"broker response is missing {field}", terminal=True
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrokerCredentialError(
                f"broker returned invalid {field}", terminal=True
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BrokerCredentialError(
                f"broker returned timezone-naive {field}", terminal=True
            )
        return parsed.astimezone(timezone.utc)

    def _request_once(self, lease_id: str) -> dict:
        if self._vend_callable is not None:
            return self._vend_callable(lease_id)
        user_token = self._user_token_supplier()
        if not isinstance(user_token, str) or not user_token:
            raise BrokerCredentialError(
                "user token supplier returned no JWT", terminal=True
            )
        response = signed_request(
            "POST",
            self.gateway_url + "/v1/credentials",
            region=self.region,
            user_token=user_token,
            aws_session=self.aws_session,
            http_client=self.http_client,
            timeout=10,
            headers={"X-Quota-Lease-Id": lease_id},
            content=b"",
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 500:
            raise BrokerCredentialError(
                f"broker returned HTTP {response.status_code}", terminal=False
            )
        if response.status_code in {403, 409, 429}:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            error_type = str(error.get("type", "broker_refused"))
            message = str(error.get("message", error_type))
            # Quota, block, expired-ID, and premature-refresh responses stop
            # this callback immediately. Botocore can invoke a later refresh;
            # consuming the broker's whole per-minute budget in one callback
            # would turn small clock skew into self-throttling.
            raise BrokerCredentialError(
                f"{error_type}: {message}",
                terminal=True,
                error_type=error_type,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerCredentialError(str(exc), terminal=True) from exc
        if not isinstance(body, dict):
            raise BrokerCredentialError(
                "broker response must be a JSON object", terminal=True
            )
        return body

    def _vend_with_retry(self, lease_id: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._request_once(lease_id)
            except (httpx.TransportError, BrokerCredentialError) as exc:
                last_error = exc
                if isinstance(exc, BrokerCredentialError) and exc.terminal:
                    raise
                if attempt + 1 == self._max_attempts:
                    break
                self._sleep(0.25 * (2**attempt))
        assert last_error is not None
        if isinstance(last_error, BrokerCredentialError):
            raise last_error
        raise BrokerCredentialError(str(last_error), terminal=False)

    def _refresh_metadata(self) -> dict[str, str]:
        # Botocore serializes this callback with its refresh lock. The local
        # lock protects lease-ID rotation for direct tests and future callers.
        with self._state_lock:
            lease_id = self._next_lease_id
            try:
                body = self._vend_with_retry(lease_id)
            except BrokerCredentialError as exc:
                if exc.error_type != "lease_expired":
                    raise
                # The broker requires a new ID after the fixed deadline. Rotate
                # once and retry; keep all transport retries for either logical
                # lease on their original ID.
                lease_id = str(uuid.uuid4())
                self._next_lease_id = lease_id
                body = self._vend_with_retry(lease_id)
            expiration = self._parse_time(body.get("expiration"), "expiration")
            refresh_raw = body.get("refresh_after")
            refresh_after = (
                self._parse_time(refresh_raw, "refresh_after")
                if refresh_raw
                else expiration - timedelta(seconds=60)
            )
            now = self._time()
            if expiration <= now:
                raise BrokerCredentialError(
                    "broker returned credentials that are already expired",
                    terminal=True,
                )
            refresh_after = min(expiration, max(now, refresh_after))
            advisory_seconds = max(
                0, int((expiration - refresh_after).total_seconds())
            )
            credentials = self._credentials_holder["credentials"]
            credentials._advisory_refresh_timeout = advisory_seconds
            credentials._mandatory_refresh_timeout = min(
                5, advisory_seconds
            )
            # Rotate only after a successful response. Transport retries use
            # the same ID, so they cannot extend an already-reserved lease.
            self._next_lease_id = str(uuid.uuid4())
            return {
                "access_key": body["aws_access_key_id"],
                "secret_key": body["aws_secret_access_key"],
                "token": body["aws_session_token"],
                "expiry_time": expiration.isoformat(),
            }

    def bedrock_client(self, **client_kwargs):
        """Return an ordinary boto3 Bedrock Runtime client using this cache."""
        botocore_session = get_session()
        botocore_session._credentials = self.credentials
        botocore_session.set_config_variable("region", self.region)
        session = boto3.Session(botocore_session=botocore_session)
        return session.client("bedrock-runtime", **client_kwargs)
