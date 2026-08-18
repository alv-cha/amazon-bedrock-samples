"""DynamoDB state used by the runtime credential broker and admin API.

Inference never passes through this application. DynamoDB stores only the
quota configuration, the event-driven daily aggregate, and a temporary map
from STS RoleSessionName to the configured JWT identity claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3

from .config import settings

MICRO = 1_000_000


def _usd_to_micro(daily_usd: float) -> int:
    micro = int(daily_usd * MICRO)
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


class QuotaStore:
    """Small read/write surface over the two stack-owned tables."""

    def __init__(self, dynamodb=None):
        self._dynamodb = dynamodb or boto3.resource(
            "dynamodb", region_name=settings.aws_region
        )
        self._users = self._dynamodb.Table(settings.users_table)
        self._usage = self._dynamodb.Table(settings.usage_table)

    def get_user(
        self, user_id: str, use_cache: bool = False
    ) -> UserRecord | None:
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
        self._users.put_item(
            Item={
                "user_id": f"SESSION#{session_name}",
                "maps_to": user_id,
                "expires_at": window_ttl_epoch(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def resolve_session(self, session_name: str) -> str | None:
        item = self._users.get_item(
            Key={"user_id": f"SESSION#{session_name}"},
            ConsistentRead=True,
        ).get("Item")
        return str(item["maps_to"]) if item else None

    def set_user_status(
        self, user_id: str, status: str, reason: str = ""
    ) -> None:
        self._users.update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET #s = :s, status_reason = :r, status_changed_at = :t"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":r": reason,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )

    def set_user_limits(
        self,
        user_id: str,
        daily_usd: float | None = None,
        daily_input_tokens: int | None = None,
        daily_output_tokens: int | None = None,
    ) -> None:
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
        return str(item.get("user_id", "")).startswith("SESSION#")

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
