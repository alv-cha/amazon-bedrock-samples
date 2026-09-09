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
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .config import settings

MICRO = 1_000_000
_SERIALIZER = TypeSerializer()
ADMIN_AUDIT_SCOPE = "routine-admin"
IDEMPOTENCY_EVENT_KEY = "REQUEST"
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
    version: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    status_origin: str = "legacy"
    lease_expires_at_epoch: int | None = None
    lease_refresh_after_epoch: int | None = None
    lease_generation: int | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class AdminMutationResult:
    user: UserRecord
    replayed: bool = False


class UserAlreadyExists(Exception):
    def __init__(self, current_user: UserRecord):
        super().__init__(f"user {current_user.user_id!r} already exists")
        self.current_user = current_user


class VersionConflict(Exception):
    def __init__(self, current_user: UserRecord):
        super().__init__(f"user {current_user.user_id!r} has changed")
        self.current_user = current_user


class IdempotencyConflict(Exception):
    """An idempotency key was already used for a different request."""


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
    """Read/write surface over quota state and isolated admin audit data."""

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
        self._admin_audit = self._dynamodb.Table(settings.admin_audit_table)
        # Transactions send TypeSerializer-encoded items and therefore need a
        # genuine low-level client. A boto3 *resource* meta client carries the
        # document-interface transform, which would re-serialize the already
        # encoded attribute values into nested maps ({"S": ...} -> {"M":
        # {"S": {"S": ...}}}) and make DynamoDB reject the item keys.
        self._client = (
            dynamodb.meta.client
            if dynamodb is not None
            else boto3.client("dynamodb", region_name=settings.aws_region)
        )
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

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        return exc.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }

    @staticmethod
    def _serialize(values: dict) -> dict:
        return {key: _SERIALIZER.serialize(value) for key, value in values.items()}

    @staticmethod
    def _encode_cursor(key: dict | None, context: dict) -> str | None:
        if not key:
            return None
        return json.dumps(
            {"version": 1, "key": key, "context": context},
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
        *,
        key_fields: set[str],
        context: dict,
    ) -> dict | None:
        if not cursor:
            return None
        try:
            envelope = json.loads(cursor)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid cursor") from exc
        if not isinstance(envelope, dict):
            raise ValueError("invalid cursor")
        if set(envelope) != {"version", "key", "context"}:
            raise ValueError("invalid cursor")
        key = envelope.get("key")
        if (
            envelope.get("version") != 1
            or envelope.get("context") != context
            or not isinstance(key, dict)
            or set(key) != key_fields
            or not all(isinstance(value, str) for value in key.values())
        ):
            raise ValueError("invalid cursor")
        return key

    def _get_user_item(self, user_id: str) -> dict | None:
        validate_user_id(user_id)
        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        return dict(item) if item else None

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
        try:
            self.put_user(
                user_id=user_id,
                name=name or user_id,
                daily_usd=settings.default_daily_usd,
                daily_input_tokens=settings.default_daily_input_tokens,
                daily_output_tokens=settings.default_daily_output_tokens,
            )
        except UserAlreadyExists:
            # A concurrent admin create is authoritative. Never replace it
            # with auto-provision defaults; return the winning row instead.
            pass
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
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "user_id": user_id,
            "name": name,
            "status": "active",
            "status_reason": "",
            "daily_usd_micro": _usd_to_micro(daily_usd),
            "daily_input_tokens": daily_input_tokens,
            "daily_output_tokens": daily_output_tokens,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "status_origin": "automatic",
        }
        try:
            self._users.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(user_id)",
            )
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            current = self.get_user(user_id)
            if current is None:
                raise
            raise UserAlreadyExists(current) from exc

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
        self,
        user_id: str,
        status: str,
        reason: str = "",
        *,
        origin: str | None = None,
        expected_version: int | None = None,
        expected_status: str | None = None,
        expected_reason: str | None = None,
    ) -> bool:
        """Write a non-admin status transition and advance config version.

        Automatic callers can bind the write to their observed configuration.
        Session, lease, warning, and vend-rate bookkeeping deliberately use
        separate writes and do not change the user configuration version.
        """
        validate_user_id(user_id)
        if origin is None:
            origin = "automatic" if reason.startswith("auto:") else "legacy"
        changed_at = datetime.now(timezone.utc).isoformat()
        values = {
            ":s": status,
            ":r": reason,
            ":t": changed_at,
            ":origin": origin,
            ":zero": 0,
            ":one": 1,
        }
        conditions: list[str] = []
        if expected_version is not None:
            values[":expected_version"] = expected_version
            conditions.append(
                "(attribute_not_exists(#version) OR "
                "#version = :expected_version)"
                if expected_version == 0
                else "#version = :expected_version"
            )
        if expected_status is not None:
            values[":expected_status"] = expected_status
            conditions.append("#s = :expected_status")
        if expected_reason is not None:
            values[":expected_reason"] = expected_reason
            conditions.append(
                "(attribute_not_exists(status_reason) OR "
                "status_reason = :expected_reason)"
                if expected_reason == ""
                else "status_reason = :expected_reason"
            )
        update_kwargs = {
            "Key": {"user_id": user_id},
            "UpdateExpression": (
                "SET #s = :s, status_reason = :r, status_changed_at = :t, "
                "updated_at = :t, status_origin = :origin, "
                "#version = if_not_exists(#version, :zero) + :one"
            ),
            "ExpressionAttributeNames": {
                "#s": "status",
                "#version": "version",
            },
            "ExpressionAttributeValues": values,
        }
        if conditions:
            update_kwargs["ConditionExpression"] = " AND ".join(conditions)
        try:
            self._users.update_item(**update_kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise
            return False
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
        return True

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
            now = datetime.now(timezone.utc).isoformat()
            sets.extend(
                [
                    "updated_at = :updated",
                    "#version = if_not_exists(#version, :zero) + :one",
                ]
            )
            values.update({":updated": now, ":zero": 0, ":one": 1})
            self._users.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET " + ", ".join(sets),
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues=values,
            )

    @staticmethod
    def _user_snapshot(user: UserRecord) -> dict:
        return {
            "user_id": user.user_id,
            "name": user.name,
            "status": user.status,
            "status_reason": user.status_reason,
            "limits": {
                "daily_usd_micro": user.daily_usd_micro,
                "daily_input_tokens": user.daily_input_tokens,
                "daily_output_tokens": user.daily_output_tokens,
            },
            "version": user.version,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "status_origin": user.status_origin,
        }

    @staticmethod
    def _snapshot_to_user(snapshot: dict) -> UserRecord:
        limits = snapshot.get("limits", {})
        return UserRecord(
            user_id=str(snapshot["user_id"]),
            name=str(snapshot.get("name", "")),
            status=str(snapshot.get("status", "active")),
            status_reason=str(snapshot.get("status_reason", "")),
            daily_usd_micro=int(limits.get("daily_usd_micro", 0)),
            daily_input_tokens=int(limits.get("daily_input_tokens", 0)),
            daily_output_tokens=int(limits.get("daily_output_tokens", 0)),
            version=int(snapshot.get("version", 0)),
            created_at=snapshot.get("created_at"),
            updated_at=snapshot.get("updated_at"),
            status_origin=str(snapshot.get("status_origin", "legacy")),
        )

    def _idempotency_result(
        self, idempotency_key: str, request_hash: str
    ) -> AdminMutationResult | None:
        marker = self._admin_audit.get_item(
            Key={
                "subject_id": f"IDEMPOTENCY#{idempotency_key}",
                "event_key": IDEMPOTENCY_EVENT_KEY,
            },
            ConsistentRead=True,
        ).get("Item")
        if not marker:
            return None
        if marker.get("request_hash") != request_hash:
            raise IdempotencyConflict(
                "idempotency key was already used for a different request"
            )
        snapshot = marker.get("result_user")
        if not isinstance(snapshot, dict):
            raise IdempotencyConflict("idempotency marker is incomplete")
        return AdminMutationResult(
            user=self._snapshot_to_user(snapshot), replayed=True
        )

    def _admin_metadata_items(
        self,
        *,
        user: UserRecord,
        before: UserRecord | None,
        event_type: str,
        actor: str,
        auth_method: str,
        reason: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> list[dict]:
        created_at = now.isoformat()
        expires_at = int(now.timestamp()) + (
            settings.admin_audit_retention_days * 86400
        )
        event_key = f"{created_at}#{uuid.uuid4()}"
        audit_item = {
            "subject_id": user.user_id,
            "event_key": event_key,
            "scope": ADMIN_AUDIT_SCOPE,
            "event_type": event_type,
            "actor": actor,
            "auth_method": auth_method,
            "reason": reason,
            "request_id": idempotency_key,
            "created_at": created_at,
            "expires_at": expires_at,
            "before": self._user_snapshot(before) if before else None,
            "after": self._user_snapshot(user),
        }
        marker_item = {
            "subject_id": f"IDEMPOTENCY#{idempotency_key}",
            "event_key": IDEMPOTENCY_EVENT_KEY,
            "scope": "idempotency",
            "request_hash": request_hash,
            "result_user": self._user_snapshot(user),
            "created_at": created_at,
            "expires_at": expires_at,
        }
        return [
            {
                "Put": {
                    "TableName": self._admin_audit.name,
                    "Item": self._serialize(audit_item),
                    "ConditionExpression": "attribute_not_exists(subject_id)",
                }
            },
            {
                "Put": {
                    "TableName": self._admin_audit.name,
                    "Item": self._serialize(marker_item),
                    "ConditionExpression": "attribute_not_exists(subject_id)",
                }
            },
        ]

    def create_admin_user(
        self,
        *,
        user_id: str,
        name: str,
        daily_usd: float,
        daily_input_tokens: int,
        daily_output_tokens: int,
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        validate_user_id(user_id)
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        item = {
            "user_id": user_id,
            "name": name,
            "status": "active",
            "status_reason": "",
            "daily_usd_micro": _usd_to_micro(daily_usd),
            "daily_input_tokens": daily_input_tokens,
            "daily_output_tokens": daily_output_tokens,
            "version": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status_origin": "admin",
        }
        user = self._to_user(item)
        transaction = [
            {
                "Put": {
                    "TableName": self._users.name,
                    "Item": self._serialize(item),
                    "ConditionExpression": "attribute_not_exists(user_id)",
                }
            },
            *self._admin_metadata_items(
                user=user,
                before=None,
                event_type="user.created",
                actor=actor,
                auth_method=auth_method,
                reason="admin user creation",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            ),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            current = self.get_user(user_id)
            if current is not None:
                raise UserAlreadyExists(current) from exc
            raise
        return AdminMutationResult(user=user)

    @staticmethod
    def _version_condition(expected_version: int) -> str:
        if expected_version == 0:
            return (
                "attribute_exists(user_id) AND "
                "(attribute_not_exists(#version) OR #version = :expected)"
            )
        return "attribute_exists(user_id) AND #version = :expected"

    def update_admin_limits(
        self,
        user_id: str,
        limits: dict,
        *,
        reason: str,
        expected_version: int,
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        current_item = self._get_user_item(user_id)
        if current_item is None:
            raise KeyError(user_id)
        current = self._to_user(current_item)
        now = now or datetime.now(timezone.utc)
        values = {
            ":expected": expected_version,
            ":next": expected_version + 1,
            ":updated": now.isoformat(),
        }
        sets = ["#version = :next", "updated_at = :updated"]
        next_item = dict(current_item)
        if "daily_usd" in limits:
            value = _usd_to_micro(limits["daily_usd"])
            values[":daily_usd"] = value
            sets.append("daily_usd_micro = :daily_usd")
            next_item["daily_usd_micro"] = value
        if "daily_input_tokens" in limits:
            value = int(limits["daily_input_tokens"])
            values[":daily_input_tokens"] = value
            sets.append("daily_input_tokens = :daily_input_tokens")
            next_item["daily_input_tokens"] = value
        if "daily_output_tokens" in limits:
            value = int(limits["daily_output_tokens"])
            values[":daily_output_tokens"] = value
            sets.append("daily_output_tokens = :daily_output_tokens")
            next_item["daily_output_tokens"] = value
        next_item.update(
            {"version": expected_version + 1, "updated_at": now.isoformat()}
        )
        updated = self._to_user(next_item)
        transaction = [
            {
                "Update": {
                    "TableName": self._users.name,
                    "Key": self._serialize({"user_id": user_id}),
                    "UpdateExpression": "SET " + ", ".join(sets),
                    "ConditionExpression": self._version_condition(
                        expected_version
                    ),
                    "ExpressionAttributeNames": {"#version": "version"},
                    "ExpressionAttributeValues": self._serialize(values),
                }
            },
            *self._admin_metadata_items(
                user=updated,
                before=current,
                event_type="user.limits.updated",
                actor=actor,
                auth_method=auth_method,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            ),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            latest = self.get_user(user_id)
            if latest is None:
                raise KeyError(user_id) from exc
            if latest.version != expected_version:
                raise VersionConflict(latest) from exc
            raise
        return AdminMutationResult(user=updated)

    def update_admin_status(
        self,
        user_id: str,
        status: str,
        reason: str,
        *,
        expected_version: int,
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        current_item = self._get_user_item(user_id)
        if current_item is None:
            raise KeyError(user_id)
        current = self._to_user(current_item)
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        next_item = dict(current_item)
        next_item.update(
            {
                "status": status,
                "status_reason": reason,
                "status_changed_at": timestamp,
                "status_origin": "admin",
                "updated_at": timestamp,
                "version": expected_version + 1,
            }
        )
        updated = self._to_user(next_item)
        values = {
            ":status": status,
            ":reason": reason,
            ":updated": timestamp,
            ":origin": "admin",
            ":expected": expected_version,
            ":next": expected_version + 1,
        }
        revocation = {
            "user_id": f"REVOCATION#{user_id}",
            "maps_to": user_id,
            "desired_status": status,
            "source_identity": str(current_item.get("source_identity", "")),
            "updated_at": timestamp,
            "expires_at": window_ttl_epoch(now),
        }
        transaction = [
            {
                "Update": {
                    "TableName": self._users.name,
                    "Key": self._serialize({"user_id": user_id}),
                    "UpdateExpression": (
                        "SET #status = :status, status_reason = :reason, "
                        "status_changed_at = :updated, updated_at = :updated, "
                        "status_origin = :origin, #version = :next"
                    ),
                    "ConditionExpression": self._version_condition(
                        expected_version
                    ),
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#version": "version",
                    },
                    "ExpressionAttributeValues": self._serialize(values),
                }
            },
            {
                "Put": {
                    "TableName": self._users.name,
                    "Item": self._serialize(revocation),
                }
            },
            *self._admin_metadata_items(
                user=updated,
                before=current,
                event_type="user.status.updated",
                actor=actor,
                auth_method=auth_method,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            ),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            latest = self.get_user(user_id)
            if latest is None:
                raise KeyError(user_id) from exc
            if latest.version != expected_version:
                raise VersionConflict(latest) from exc
            raise
        return AdminMutationResult(user=updated)

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
            version=int(item.get("version", 0)),
            created_at=(
                str(item["created_at"]) if item.get("created_at") else None
            ),
            updated_at=(
                str(item["updated_at"]) if item.get("updated_at") else None
            ),
            status_origin=str(item.get("status_origin", "legacy")),
            lease_expires_at_epoch=(
                int(item["lease_expires_at_epoch"])
                if item.get("lease_expires_at_epoch") is not None
                else None
            ),
            lease_refresh_after_epoch=(
                int(item["lease_refresh_after_epoch"])
                if item.get("lease_refresh_after_epoch") is not None
                else None
            ),
            lease_generation=(
                int(item["lease_generation"])
                if item.get("lease_generation") is not None
                else None
            ),
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

    @staticmethod
    def _automatic_status_owned(user: UserRecord) -> bool:
        return user.status_origin == "automatic" or (
            user.status_origin == "legacy"
            and user.status_reason.startswith("auto:")
        )

    def refresh_auto_status(self, user: UserRecord) -> UserRecord:
        """Reactivate an owned automatic block after reset/limit increase."""
        current = user
        for _ in range(3):
            if (
                current.active
                or not self._automatic_status_owned(current)
                or self.is_over_budget(current)
            ):
                return current
            changed = self.set_user_status(
                current.user_id,
                "active",
                "auto: current window is under quota",
                expected_version=current.version,
                expected_status=current.status,
                expected_reason=current.status_reason,
            )
            refreshed = self.get_user(current.user_id)
            assert refreshed is not None
            if changed:
                return refreshed
            current = refreshed
        return current

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
        self,
        limit: int = 50,
        cursor: str | None = None,
        *,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[list[UserRecord], str | None]:
        normalized_query = (query or "").strip().casefold()
        cursor_context = {
            "kind": "users",
            "status": status or "",
            "query": normalized_query,
        }
        exclusive_key = self._decode_cursor(
            cursor,
            key_fields={"user_id"},
            context=cursor_context,
        )
        users: list[UserRecord] = []
        evaluated = 0
        max_evaluated = min(1000, max(limit * 10, 100))
        next_key = exclusive_key
        while len(users) < limit and evaluated < max_evaluated:
            chunk = min(max(limit - len(users), 1), max_evaluated - evaluated)
            scan_kwargs: dict = {"Limit": chunk}
            if next_key:
                scan_kwargs["ExclusiveStartKey"] = next_key
            try:
                response = self._users.scan(**scan_kwargs)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != (
                    "ValidationException"
                ):
                    raise
                raise ValueError("invalid cursor") from exc
            items = response.get("Items", [])
            evaluated += len(items)
            for item in items:
                if self._is_sentinel(item):
                    continue
                user = self._to_user(item)
                if status and user.status != status:
                    continue
                if normalized_query and normalized_query not in (
                    f"{user.user_id}\n{user.name}".casefold()
                ):
                    continue
                users.append(user)
                if len(users) == limit:
                    break
            next_key = response.get("LastEvaluatedKey")
            if not next_key:
                break
        return users, self._encode_cursor(next_key, cursor_context)

    def get_usage_history_page(
        self,
        user_id: str,
        *,
        start: str,
        end: str,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        query_kwargs: dict = {
            "KeyConditionExpression": (
                "user_id = :user_id AND #window BETWEEN :start AND :end"
            ),
            "ExpressionAttributeNames": {"#window": "window"},
            "ExpressionAttributeValues": {
                ":user_id": user_id,
                ":start": start,
                ":end": end,
            },
            "ScanIndexForward": False,
            "Limit": limit,
        }
        cursor_context = {
            "kind": "usage-history",
            "user_id": user_id,
            "start": start,
            "end": end,
        }
        key = self._decode_cursor(
            cursor,
            key_fields={"user_id", "window"},
            context=cursor_context,
        )
        if key is not None:
            if (
                key["user_id"] != user_id
                or not start <= key["window"] <= end
            ):
                raise ValueError("invalid cursor")
            query_kwargs["ExclusiveStartKey"] = key
        try:
            response = self._usage.query(**query_kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ValidationException"
            ):
                raise
            raise ValueError("invalid cursor") from exc
        history = [
            {
                "user_id": user_id,
                "window": str(item["window"]),
                "cost_usd": int(item.get("cost_micro", 0)) / MICRO,
                "input_tokens": int(item.get("input_tokens", 0)),
                "output_tokens": int(item.get("output_tokens", 0)),
                "requests": int(item.get("requests", 0)),
            }
            for item in response.get("Items", [])
        ]
        last = response.get("LastEvaluatedKey")
        return history, self._encode_cursor(last, cursor_context)

    @staticmethod
    def _json_safe(value):
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, dict):
            return {
                key: QuotaStore._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [QuotaStore._json_safe(item) for item in value]
        return value

    @staticmethod
    def _public_audit_event(item: dict) -> dict:
        return {
            "user_id": str(item.get("subject_id", "")),
            "event_key": str(item.get("event_key", "")),
            "event_type": str(item.get("event_type", "")),
            "actor": str(item.get("actor", "")),
            "auth_method": str(item.get("auth_method", "")),
            "reason": str(item.get("reason", "")),
            "request_id": str(item.get("request_id", "")),
            "created_at": str(item.get("created_at", "")),
            "before": QuotaStore._json_safe(item.get("before")),
            "after": QuotaStore._json_safe(item.get("after")),
        }

    def list_admin_audit_page(
        self,
        *,
        user_id: str | None,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        if user_id:
            query_kwargs: dict = {
                "KeyConditionExpression": "subject_id = :subject_id",
                "ExpressionAttributeValues": {":subject_id": user_id},
                "ScanIndexForward": False,
                "Limit": limit,
            }
        else:
            query_kwargs = {
                "IndexName": "scope-event-key-index",
                "KeyConditionExpression": "#scope = :scope",
                "ExpressionAttributeNames": {"#scope": "scope"},
                "ExpressionAttributeValues": {":scope": ADMIN_AUDIT_SCOPE},
                "ScanIndexForward": False,
                "Limit": limit,
            }
        cursor_context = {
            "kind": "admin-audit",
            "user_id": user_id or "",
        }
        key_fields = (
            {"subject_id", "event_key"}
            if user_id
            else {"subject_id", "event_key", "scope"}
        )
        key = self._decode_cursor(
            cursor,
            key_fields=key_fields,
            context=cursor_context,
        )
        if key is not None:
            if user_id and key["subject_id"] != user_id:
                raise ValueError("invalid cursor")
            if not user_id and key["scope"] != ADMIN_AUDIT_SCOPE:
                raise ValueError("invalid cursor")
            query_kwargs["ExclusiveStartKey"] = key
        try:
            response = self._admin_audit.query(**query_kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ValidationException"
            ):
                raise
            raise ValueError("invalid cursor") from exc
        events = [
            self._public_audit_event(item)
            for item in response.get("Items", [])
            if item.get("scope") == ADMIN_AUDIT_SCOPE
        ]
        last = response.get("LastEvaluatedKey")
        return events, self._encode_cursor(last, cursor_context)
