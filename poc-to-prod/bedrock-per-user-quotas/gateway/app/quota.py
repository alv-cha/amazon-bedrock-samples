"""DynamoDB state used by the runtime credential broker and admin API.

Inference never passes through this application. DynamoDB stores only the
quota configuration, the event-driven daily aggregate, and a temporary map
from STS RoleSessionName to the configured JWT identity claim.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from .config import settings

MICRO = 1_000_000
RESERVED_USER_ID_PREFIXES = (
    "SESSION#",
    "VEND#",
    "REVOCATION#",
    "CONFIG#",
    "EMERGENCY_AUDIT#",
)


def validate_user_id(user_id: str) -> str:
    if not user_id or any(
        user_id.startswith(prefix) for prefix in RESERVED_USER_ID_PREFIXES
    ):
        raise ValueError("user identity uses a reserved internal prefix")
    return user_id


def _usd_to_micro(daily_usd: float) -> int:
    # Admin/API values are decimal USD but arrive as binary floats. Round to
    # the nearest micro-dollar instead of truncating values such as 1.000044
    # to 1.000043 due to representation error.
    micro = int(round(daily_usd * MICRO))
    if daily_usd > 0 and micro == 0:
        return 1
    return micro


def current_window(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def window_ttl_epoch(
    now: datetime | None = None, keep_days: int | None = None
) -> int:
    now = now or datetime.now(timezone.utc)
    keep_days = keep_days or settings.usage_retention_days
    return int(now.timestamp()) + keep_days * 86400


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    name: str
    status: str
    status_reason: str
    daily_usd_micro: int
    daily_input_tokens: int
    daily_output_tokens: int

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class LeaseReservation:
    lease_id: str
    generation: int
    expires_at: datetime
    refresh_after: datetime
    created: bool


class LeaseNotRefreshable(Exception):
    def __init__(self, retry_after: datetime):
        super().__init__("logical lease is not in its refresh window")
        self.retry_after = retry_after


class LeaseRateLimited(Exception):
    def __init__(self, retry_after: datetime):
        super().__init__("credential vend rate limit exceeded")
        self.retry_after = retry_after


class LeaseExpired(Exception):
    """A retry used a lease ID whose fixed deadline has already passed."""


class QuotaStore:
    """Small read/write surface over the two stack-owned tables."""

    def __init__(
        self,
        dynamodb=None,
        *,
        lease_seconds: int | None = None,
        refresh_overlap_seconds: int | None = None,
        refresh_jitter_seconds: int | None = None,
        vend_rate_limit_per_minute: int | None = None,
        jitter_fn: Callable[[int], int] | None = None,
    ):
        self._dynamodb = dynamodb or boto3.resource(
            "dynamodb", region_name=settings.aws_region
        )
        self._users = self._dynamodb.Table(settings.users_table)
        self._usage = self._dynamodb.Table(settings.usage_table)
        self._lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else settings.permission_lease_seconds
        )
        self._refresh_overlap_seconds = (
            refresh_overlap_seconds
            if refresh_overlap_seconds is not None
            else settings.refresh_overlap_seconds
        )
        self._refresh_jitter_seconds = (
            refresh_jitter_seconds
            if refresh_jitter_seconds is not None
            else settings.refresh_jitter_seconds
        )
        self._vend_rate_limit_per_minute = (
            vend_rate_limit_per_minute
            if vend_rate_limit_per_minute is not None
            else settings.vend_rate_limit_per_minute
        )
        self._jitter = jitter_fn or (
            lambda maximum: secrets.randbelow(maximum + 1)
            if maximum > 0
            else 0
        )

    def get_user(
        self, user_id: str, use_cache: bool = False
    ) -> UserRecord | None:
        validate_user_id(user_id)
        del use_cache  # Kept as a compatibility argument for existing callers.
        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        return self._to_user(item) if item else None

    def get_or_provision_user(
        self, user_id: str, name: str = ""
    ) -> UserRecord:
        user = self.get_user(user_id)
        if user is not None:
            return user
        self.put_user(
            user_id=user_id,
            name=name or user_id,
            daily_usd=settings.default_daily_usd,
            daily_input_tokens=settings.default_daily_input_tokens,
            daily_output_tokens=settings.default_daily_output_tokens,
        )
        user = self.get_user(user_id)
        assert user is not None
        return user

    def put_user(
        self,
        user_id: str,
        name: str,
        daily_usd: float,
        daily_input_tokens: int,
        daily_output_tokens: int,
    ) -> None:
        validate_user_id(user_id)
        self._users.put_item(
            Item={
                "user_id": user_id,
                "name": name,
                "status": "active",
                "status_reason": "",
                "daily_usd_micro": _usd_to_micro(daily_usd),
                "daily_input_tokens": daily_input_tokens,
                "daily_output_tokens": daily_output_tokens,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def record_session(self, session_name: str, user_id: str) -> None:
        validate_user_id(user_id)
        now = datetime.now(timezone.utc).isoformat()
        self._users.put_item(
            Item={
                "user_id": f"SESSION#{session_name}",
                "maps_to": user_id,
                "expires_at": window_ttl_epoch(),
                "updated_at": now,
            }
        )
        # Persist the exact value stamped into SourceIdentity. A future
        # revocation worker must never duplicate the sanitizer and drift from
        # the identity attached to existing sessions.
        self._users.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET source_identity = :s, session_updated_at = :t",
            ExpressionAttributeValues={":s": session_name, ":t": now},
        )

    def resolve_session(self, session_name: str) -> str | None:
        item = self._users.get_item(
            Key={"user_id": f"SESSION#{session_name}"},
            ConsistentRead=True,
        ).get("Item")
        return str(item["maps_to"]) if item else None

    @staticmethod
    def _lease_from_item(item: dict, *, created: bool) -> LeaseReservation | None:
        lease_id = item.get("lease_id")
        expires_epoch = item.get("lease_expires_at_epoch")
        refresh_epoch = item.get("lease_refresh_after_epoch")
        generation = item.get("lease_generation")
        if not all(
            value is not None
            for value in (lease_id, expires_epoch, refresh_epoch, generation)
        ):
            return None
        return LeaseReservation(
            lease_id=str(lease_id),
            generation=int(generation),
            expires_at=datetime.fromtimestamp(
                int(expires_epoch), tz=timezone.utc
            ),
            refresh_after=datetime.fromtimestamp(
                int(refresh_epoch), tz=timezone.utc
            ),
            created=created,
        )

    def get_active_lease(self, user_id: str) -> LeaseReservation | None:
        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item", {})
        return self._lease_from_item(item, created=False)

    def _consume_vend_rate(self, user_id: str, now: datetime) -> None:
        minute = now.strftime("%Y%m%dT%H%M")
        retry_after = now.replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        try:
            self._users.update_item(
                Key={"user_id": f"VEND#{user_id}#{minute}"},
                UpdateExpression=(
                    "ADD vend_count :one SET expires_at = :ttl"
                ),
                ConditionExpression=(
                    "attribute_not_exists(vend_count) OR vend_count < :limit"
                ),
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": self._vend_rate_limit_per_minute,
                    ":ttl": int(retry_after.timestamp()) + 120,
                },
            )
        except ClientError as exc:
            if (
                exc.response.get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise
            raise LeaseRateLimited(retry_after) from exc

    def reserve_lease(
        self,
        user_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
        expires_no_later_than: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> LeaseReservation:
        """Reserve or retry one fixed permission lease atomically.

        A client-generated lease ID makes retries non-extending: replacement
        STS credentials receive the original deadline. A different ID is only
        accepted once the current lease enters its controlled refresh window.
        """
        now = now or datetime.now(timezone.utc)
        validate_user_id(user_id)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("lease clock must be timezone-aware")
        if not lease_id or len(lease_id) > 128:
            raise ValueError("lease ID must contain 1 to 128 characters")
        self._consume_vend_rate(user_id, now)

        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        if not item:
            raise KeyError(f"user {user_id!r} was not found")
        current = self._lease_from_item(item, created=False)
        if current and current.lease_id == lease_id:
            if now >= current.expires_at:
                raise LeaseExpired("logical lease has expired; use a new ID")
            return current
        if current and now < current.refresh_after:
            raise LeaseNotRefreshable(current.refresh_after)

        duration = lease_seconds or self._lease_seconds
        expires_at = now + timedelta(seconds=duration)
        if expires_no_later_than is not None:
            if (
                expires_no_later_than.tzinfo is None
                or expires_no_later_than.utcoffset() is None
            ):
                raise ValueError("lease expiration cap must be timezone-aware")
            expires_at = min(expires_at, expires_no_later_than)
        if expires_at <= now:
            raise LeaseExpired("logical lease deadline is not in the future")
        jitter = self._jitter(self._refresh_jitter_seconds)
        if not 0 <= jitter <= self._refresh_jitter_seconds:
            raise ValueError("lease jitter source returned an invalid value")
        refresh_after = max(
            now,
            expires_at
            - timedelta(seconds=self._refresh_overlap_seconds)
            + timedelta(seconds=jitter),
        )
        previous_generation = int(item.get("lease_generation", 0))
        generation = previous_generation + 1
        condition = "attribute_not_exists(lease_id)"
        if current is not None:
            condition = (
                "lease_refresh_after_epoch <= :now AND "
                "lease_generation = :previous_generation"
            )
        values = {
            ":lease_id": lease_id,
            ":generation": generation,
            ":expires": int(expires_at.timestamp()),
            ":refresh": int(refresh_after.timestamp()),
            ":updated": now.isoformat(),
        }
        if current is not None:
            values.update(
                {
                    ":now": int(now.timestamp()),
                    ":previous_generation": previous_generation,
                }
            )
        try:
            response = self._users.update_item(
                Key={"user_id": user_id},
                UpdateExpression=(
                    "SET lease_id = :lease_id, "
                    "lease_generation = :generation, "
                    "lease_expires_at_epoch = :expires, "
                    "lease_refresh_after_epoch = :refresh, "
                    "lease_updated_at = :updated"
                ),
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if (
                exc.response.get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise
            winner = self.get_active_lease(user_id)
            if winner and winner.lease_id == lease_id:
                return winner
            raise LeaseNotRefreshable(
                winner.refresh_after if winner else now
            ) from exc
        reservation = self._lease_from_item(
            response["Attributes"], created=True
        )
        assert reservation is not None
        return reservation

    def get_emergency_state(self) -> dict:
        item = self._users.get_item(
            Key={"user_id": "CONFIG#EMERGENCY_STOP"},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return {
                "state": "inactive",
                "desired_active": False,
                "generation": 0,
                "actor": "",
                "reason": "",
            }
        return dict(item)

    def emergency_stop_active(self) -> bool:
        state = self.get_emergency_state()
        return bool(state.get("desired_active")) or state.get("state") != "inactive"

    def set_emergency_desired(
        self,
        *,
        active: bool,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        current = self.get_emergency_state()
        generation = int(current.get("generation", 0)) + 1
        request_id = str(uuid.uuid4())
        state = "activating" if active else "recovering"
        item = {
            "user_id": "CONFIG#EMERGENCY_STOP",
            "state": state,
            "desired_active": active,
            "generation": generation,
            "actor": actor,
            "reason": reason,
            "request_id": request_id,
            "requested_at": now.isoformat(),
        }
        # Write the immutable audit row first. A later state-write failure can
        # leave a harmless unapplied request record, never an unaudited action.
        self._users.put_item(
            Item={
                "user_id": f"EMERGENCY_AUDIT#{now.isoformat()}#{request_id}",
                "action": "activate" if active else "recover",
                "actor": actor,
                "reason": reason,
                "generation": generation,
                "requested_at": now.isoformat(),
                "expires_at": window_ttl_epoch(now),
            }
        )
        self._users.put_item(Item=item)
        return dict(item)

    def mark_emergency_applied(
        self,
        *,
        active: bool,
        generation: int | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if generation is None:
            generation = int(self.get_emergency_state().get("generation", 0))
        self._users.update_item(
            Key={"user_id": "CONFIG#EMERGENCY_STOP"},
            UpdateExpression=(
                "SET #s = :s, applied_at = :t, "
                "applied_generation = :generation"
            ),
            ConditionExpression=(
                "desired_active = :desired AND generation = :generation"
            ),
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":s": "active" if active else "inactive",
                ":t": now.isoformat(),
                ":desired": active,
                ":generation": generation,
            },
        )

    def set_user_status(
        self, user_id: str, status: str, reason: str = ""
    ) -> None:
        validate_user_id(user_id)
        changed_at = datetime.now(timezone.utc).isoformat()
        self._users.update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET #s = :s, status_reason = :r, status_changed_at = :t"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":r": reason,
                ":t": changed_at,
            },
        )
        user = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item", {})
        self._users.put_item(
            Item={
                "user_id": f"REVOCATION#{user_id}",
                "maps_to": user_id,
                "desired_status": status,
                "source_identity": str(user.get("source_identity", "")),
                "updated_at": changed_at,
                "expires_at": window_ttl_epoch(),
            }
        )

    def set_user_limits(
        self,
        user_id: str,
        daily_usd: float | None = None,
        daily_input_tokens: int | None = None,
        daily_output_tokens: int | None = None,
    ) -> None:
        validate_user_id(user_id)
        sets: list[str] = []
        values: dict = {}
        if daily_usd is not None:
            sets.append("daily_usd_micro = :c")
            values[":c"] = _usd_to_micro(daily_usd)
        if daily_input_tokens is not None:
            sets.append("daily_input_tokens = :i")
            values[":i"] = daily_input_tokens
        if daily_output_tokens is not None:
            sets.append("daily_output_tokens = :o")
            values[":o"] = daily_output_tokens
        if sets:
            self._users.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET " + ", ".join(sets),
                ExpressionAttributeValues=values,
            )

    @staticmethod
    def _to_user(item: dict) -> UserRecord:
        return UserRecord(
            user_id=str(item["user_id"]),
            name=str(item.get("name", "")),
            status=str(item.get("status", "active")),
            status_reason=str(item.get("status_reason", "")),
            daily_usd_micro=int(item.get("daily_usd_micro", 0)),
            daily_input_tokens=int(item.get("daily_input_tokens", 0)),
            daily_output_tokens=int(item.get("daily_output_tokens", 0)),
        )

    def get_window_usage(
        self, user_id: str, window: str | None = None
    ) -> dict:
        window = window or current_window()
        item = self._usage.get_item(
            Key={"user_id": user_id, "window": window},
            ConsistentRead=True,
        ).get("Item", {})
        return {
            "user_id": user_id,
            "window": window,
            "cost_usd": int(item.get("cost_micro", 0)) / MICRO,
            "input_tokens": int(item.get("input_tokens", 0)),
            "output_tokens": int(item.get("output_tokens", 0)),
            "requests": int(item.get("requests", 0)),
        }

    def is_over_budget(self, user: UserRecord) -> bool:
        usage = self.get_window_usage(user.user_id)
        cost_micro = int(round(usage["cost_usd"] * MICRO))
        return bool(
            (user.daily_usd_micro and cost_micro >= user.daily_usd_micro)
            or (
                user.daily_input_tokens
                and usage["input_tokens"] >= user.daily_input_tokens
            )
            or (
                user.daily_output_tokens
                and usage["output_tokens"] >= user.daily_output_tokens
            )
        )

    def refresh_auto_status(self, user: UserRecord) -> UserRecord:
        """Reactivate a prior automatic block after reset or a limit increase."""
        if (
            not user.active
            and user.status_reason.startswith("auto:")
            and not self.is_over_budget(user)
        ):
            self.set_user_status(
                user.user_id, "active", "auto: current window is under quota"
            )
            refreshed = self.get_user(user.user_id)
            assert refreshed is not None
            return refreshed
        return user

    @staticmethod
    def _is_sentinel(item: dict) -> bool:
        user_id = str(item.get("user_id", ""))
        return user_id.startswith(
            (
                "SESSION#",
                "VEND#",
                "REVOCATION#",
                "CONFIG#",
                "EMERGENCY_AUDIT#",
            )
        )

    def list_users(self) -> list[UserRecord]:
        users: list[UserRecord] = []
        response = self._users.scan()
        users.extend(
            self._to_user(item)
            for item in response.get("Items", [])
            if not self._is_sentinel(item)
        )
        while "LastEvaluatedKey" in response:
            response = self._users.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            users.extend(
                self._to_user(item)
                for item in response.get("Items", [])
                if not self._is_sentinel(item)
            )
        return users

    def list_users_page(
        self, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[UserRecord], str | None]:
        scan_kwargs: dict = {"Limit": max(1, limit)}
        if cursor:
            try:
                scan_kwargs["ExclusiveStartKey"] = json.loads(cursor)
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid cursor") from exc
        response = self._users.scan(**scan_kwargs)
        users = [
            self._to_user(item)
            for item in response.get("Items", [])
            if not self._is_sentinel(item)
        ]
        last = response.get("LastEvaluatedKey")
        return users, json.dumps(last) if last else None
