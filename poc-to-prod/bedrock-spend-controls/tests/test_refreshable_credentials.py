from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from examples import refreshable_bedrock as refreshable_module
from examples.refreshable_bedrock import (
    BrokerCredentialError,
    QuotaBrokerCredentialProvider,
)


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def _response(clock: Clock, key: str, lease_id: str) -> dict:
    return {
        "aws_access_key_id": key,
        "aws_secret_access_key": "secret",
        "aws_session_token": "token",
        "expiration": (clock.now + timedelta(seconds=60)).isoformat(),
        "sts_expiration": (clock.now + timedelta(minutes=15)).isoformat(),
        "refresh_after": (clock.now + timedelta(seconds=50)).isoformat(),
        "lease_id": lease_id,
    }


def test_provider_is_lazy_and_concurrent_first_use_is_single_flight():
    clock = Clock()
    calls: list[str] = []

    def vend(lease_id: str) -> dict:
        calls.append(lease_id)
        return _response(clock, "ASIA1", lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        time_fetcher=clock,
    )
    assert calls == []

    with ThreadPoolExecutor(max_workers=8) as executor:
        frozen = list(
            executor.map(
                lambda _: provider.credentials.get_frozen_credentials(),
                range(8),
            )
        )

    assert len(calls) == 1
    assert {credentials.access_key for credentials in frozen} == {"ASIA1"}


def test_many_uses_reuse_credentials_then_refresh_with_new_logical_lease():
    clock = Clock()
    calls: list[str] = []

    def vend(lease_id: str) -> dict:
        calls.append(lease_id)
        return _response(clock, f"ASIA{len(calls)}", lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        time_fetcher=clock,
    )

    first = provider.credentials.get_frozen_credentials()
    for _ in range(20):
        assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    assert len(calls) == 1

    clock.now += timedelta(seconds=51)
    refreshed = provider.credentials.get_frozen_credentials()

    assert refreshed.access_key == "ASIA2"
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert first.access_key != refreshed.access_key


def test_transport_retry_reuses_same_lease_id_without_extension():
    clock = Clock()
    attempts: list[str] = []
    sleeps: list[float] = []

    def vend(lease_id: str) -> dict:
        attempts.append(lease_id)
        if len(attempts) == 1:
            raise BrokerCredentialError("temporary", terminal=False)
        return _response(clock, "ASIA2", lease_id)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        time_fetcher=clock,
        sleep_fn=sleeps.append,
    )

    credentials = provider.credentials.get_frozen_credentials()

    assert credentials.access_key == "ASIA2"
    assert attempts[0] == attempts[1]
    assert sleeps == [0.25]


def test_terminal_block_does_not_retry_or_return_credentials():
    attempts = 0

    def vend(_lease_id: str) -> dict:
        nonlocal attempts
        attempts += 1
        raise BrokerCredentialError("quota_blocked", terminal=True)

    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        vend_callable=vend,
        sleep_fn=lambda _: None,
    )

    with pytest.raises(BrokerCredentialError, match="quota_blocked"):
        provider.credentials.get_frozen_credentials()
    assert attempts == 1


def test_provider_rejects_expired_or_malformed_broker_response():
    clock = Clock()

    for response in (
        {},
        {
            "aws_access_key_id": "ASIA",
            "aws_secret_access_key": "secret",
            "aws_session_token": "token",
            "expiration": (clock.now - timedelta(seconds=1)).isoformat(),
        },
    ):
        provider = QuotaBrokerCredentialProvider(
            "https://broker.example",
            "jwt",
            vend_callable=lambda _lease_id, response=response: response,
            time_fetcher=clock,
        )
        with pytest.raises(BrokerCredentialError):
            provider.credentials.get_frozen_credentials()


def test_live_refresh_asks_token_supplier_for_each_logical_lease(monkeypatch):
    clock = Clock()
    supplied = iter(["jwt-1", "jwt-2"])
    seen_tokens: list[str] = []
    calls = 0

    class Response:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        seen_tokens.append(kwargs["user_token"])
        return Response(_response(clock, f"ASIA{calls}", kwargs["headers"]["X-Quota-Lease-Id"]))

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        lambda: next(supplied),
        time_fetcher=clock,
    )

    assert provider.credentials.get_frozen_credentials().access_key == "ASIA1"
    clock.now += timedelta(seconds=51)
    assert provider.credentials.get_frozen_credentials().access_key == "ASIA2"
    assert seen_tokens == ["jwt-1", "jwt-2"]


def test_premature_refresh_does_not_consume_retry_budget(monkeypatch):
    calls = 0

    class Response:
        status_code = 429

        def json(self):
            return {
                "error": {
                    "type": "lease_not_refreshable",
                    "message": "wait for refresh window",
                }
            }

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        sleep_fn=lambda _: pytest.fail("must not immediately retry 429"),
        max_attempts=3,
    )

    with pytest.raises(BrokerCredentialError, match="lease_not_refreshable"):
        provider.credentials.get_frozen_credentials()
    assert calls == 1


def test_expired_lease_id_rotates_once_and_recovers(monkeypatch):
    clock = Clock()
    lease_ids: list[str] = []

    class Response:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            return None

    def signed_request(*args, **kwargs):
        lease_id = kwargs["headers"]["X-Quota-Lease-Id"]
        lease_ids.append(lease_id)
        if len(lease_ids) == 1:
            return Response(
                409,
                {
                    "error": {
                        "type": "lease_expired",
                        "message": "use a new lease ID",
                    }
                },
            )
        return Response(200, _response(clock, "ASIA2", lease_id))

    monkeypatch.setattr(refreshable_module, "signed_request", signed_request)
    provider = QuotaBrokerCredentialProvider(
        "https://broker.example",
        "jwt",
        time_fetcher=clock,
        sleep_fn=lambda _: pytest.fail("409 rotation should not back off"),
    )

    credentials = provider.credentials.get_frozen_credentials()

    assert credentials.access_key == "ASIA2"
    assert len(lease_ids) == 2
    assert lease_ids[0] != lease_ids[1]
