"""Per-user quota accounting on DynamoDB with a reserve -> settle protocol.

Why reserve/settle instead of check-then-increment?
  A pure post-hoc counter lets a user fire N concurrent requests that each
  pass the check before any of them is counted. Reserving the *worst case*
  (estimated input + max output tokens, priced) atomically at admission
  closes that hole; when the response completes we settle the counters down
  to the real usage. This mirrors how the bedrock-mantle endpoint itself
  admits requests against its TPM quotas.

Tables (created by the CDK stack):

  users:  user_id (PK) | name, status, daily_usd_micro,
          daily_input_tokens, daily_output_tokens
          (user_id is the verified JWT claim, default "sub")
  usage:  user_id (PK), window (SK, "YYYY-MM-DD") | cost_micro,
          input_tokens, output_tokens, requests, throttles, expires_at (TTL)
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from .config import settings
from .pricing import MICRO, cost_micro_usd

# A per-dimension limit of 0 means "not enforced" (unlimited) for that
# dimension, so a customer can cap on USD alone by zeroing the token limits.
# To stop a user entirely, block them (status), don't set a 0 budget. We use
# a large sentinel headroom for unlimited dimensions so the atomic reserve
# condition treats them as non-binding rather than always-failing.
_UNLIMITED_HEADROOM = 1 << 62


def _usd_to_micro(daily_usd: float) -> int:
    """USD budget -> integer micro-USD. Any *positive* budget floors to at
    least 1 micro, so a tiny cap (e.g. $0.0000004) can't round to 0 and get
    mistaken for the "0 = unlimited" sentinel. Exactly 0 stays 0 (unlimited).
    """
    micro = int(daily_usd * MICRO)
    if daily_usd > 0 and micro == 0:
        return 1
    return micro


def current_window(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def window_ttl_epoch(now: datetime | None = None, keep_days: int | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    keep_days = keep_days or settings.usage_retention_days
    return int(now.timestamp()) + keep_days * 86400


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    name: str
    status: str
    daily_usd_micro: int
    daily_input_tokens: int
    daily_output_tokens: int
    # Optional per-user Bedrock Project for Mantle (Mode B) cost attribution;
    # empty means fall back to settings.default_mantle_project_id.
    mantle_project_id: str = ""

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class Reservation:
    user_id: str
    window: str
    model_id: str
    reserved_cost_micro: int
    reserved_input_tokens: int
    reserved_output_tokens: int


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    reason: str = ""
    reservation: Reservation | None = None
    # Snapshot for X-Quota-* response headers (best effort).
    remaining_usd_micro: int | None = None


class QuotaExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class QuotaStore:
    """DynamoDB-backed store. The boto3 resource is injectable for tests."""

    def __init__(self, dynamodb=None):
        self._dynamodb = dynamodb or boto3.resource("dynamodb", region_name=settings.aws_region)
        self._users = self._dynamodb.Table(settings.users_table)
        self._usage = self._dynamodb.Table(settings.usage_table)
        # Tiny TTL cache so hot users don't cost a GSI query per request.
        self._user_cache: dict[str, tuple[float, UserRecord]] = {}
        self._user_cache_ttl = 15.0

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def get_user(self, user_id: str, use_cache: bool = False) -> UserRecord | None:
        if use_cache:
            cached = self._user_cache.get(user_id)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        resp = self._users.get_item(Key={"user_id": user_id})
        item = resp.get("Item")
        if not item:
            return None
        user = self._to_user(item)
        self._user_cache[user_id] = (time.monotonic() + self._user_cache_ttl, user)
        return user

    def get_or_provision_user(self, user_id: str, name: str = "") -> UserRecord:
        """Fetch the user record; create it with default limits on first sight.

        With JWT auth the user population lives in the customer's IdP — the
        gateway only needs a limits/status record per user id, so new users
        are provisioned lazily (when AUTO_PROVISION_USERS allows it, which
        the caller checks).
        """
        user = self.get_user(user_id, use_cache=True)
        if user is not None:
            return user
        self.put_user(
            user_id=user_id,
            name=name or user_id,
            daily_usd=settings.default_daily_usd,
            daily_input_tokens=settings.default_daily_input_tokens,
            daily_output_tokens=settings.default_daily_output_tokens,
        )
        return self.get_user(user_id)

    def put_user(self, user_id: str, name: str,
                 daily_usd: float, daily_input_tokens: int, daily_output_tokens: int,
                 mantle_project_id: str = "") -> None:
        item = {
            "user_id": user_id,
            "name": name,
            "status": "active",
            "daily_usd_micro": _usd_to_micro(daily_usd),
            "daily_input_tokens": daily_input_tokens,
            "daily_output_tokens": daily_output_tokens,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if mantle_project_id:
            item["mantle_project_id"] = mantle_project_id
        self._users.put_item(Item=item)
        self._user_cache.pop(user_id, None)

    def record_session(self, session_name: str, user_id: str) -> None:
        """Persist the RoleSessionName -> user_id (full JWT sub) mapping.

        The broker sanitizes/hashes the sub into a session name that is what
        actually appears in Bedrock model-invocation logs (inside the
        assumed-role ARN). The reconciler reads spend keyed by session name,
        so it needs this reverse map to attribute usage back to the real
        user id / budget row. Stored as a sentinel item in the users table
        (PK "SESSION#<name>") to avoid a second table; TTL'd so stale
        sessions self-clean.
        """
        self._users.put_item(Item={
            "user_id": f"SESSION#{session_name}",
            "maps_to": user_id,
            "expires_at": window_ttl_epoch(keep_days=2),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def resolve_session(self, session_name: str) -> str | None:
        """Reverse a RoleSessionName back to the user id, or None if unknown."""
        resp = self._users.get_item(Key={"user_id": f"SESSION#{session_name}"})
        item = resp.get("Item")
        return str(item["maps_to"]) if item else None

    def set_user_status(self, user_id: str, status: str, reason: str = "") -> None:
        self._users.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET #s = :s, status_reason = :r, status_changed_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":r": reason,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._user_cache.clear()

    def set_user_mantle_project(self, user_id: str, project_id: str) -> None:
        """Set (or clear, with "") the user's managed Bedrock Project for
        Mantle cost attribution. Empty string removes the per-user mapping so
        the gateway falls back to settings.default_mantle_project_id."""
        if project_id:
            self._users.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET mantle_project_id = :p",
                ExpressionAttributeValues={":p": project_id},
            )
        else:
            self._users.update_item(
                Key={"user_id": user_id},
                UpdateExpression="REMOVE mantle_project_id",
            )
        self._user_cache.clear()

    def set_user_limits(self, user_id: str, daily_usd: float | None = None,
                        daily_input_tokens: int | None = None,
                        daily_output_tokens: int | None = None) -> None:
        sets, values = [], {}
        if daily_usd is not None:
            sets.append("daily_usd_micro = :c")
            values[":c"] = _usd_to_micro(daily_usd)
        if daily_input_tokens is not None:
            sets.append("daily_input_tokens = :i")
            values[":i"] = daily_input_tokens
        if daily_output_tokens is not None:
            sets.append("daily_output_tokens = :o")
            values[":o"] = daily_output_tokens
        if not sets:
            return
        self._users.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=values,
        )
        self._user_cache.clear()

    @staticmethod
    def _to_user(item: dict) -> UserRecord:
        return UserRecord(
            user_id=str(item["user_id"]),
            name=str(item.get("name", "")),
            status=str(item.get("status", "active")),
            daily_usd_micro=int(item.get("daily_usd_micro", 0)),
            daily_input_tokens=int(item.get("daily_input_tokens", 0)),
            daily_output_tokens=int(item.get("daily_output_tokens", 0)),
            mantle_project_id=str(item.get("mantle_project_id", "")),
        )

    # ------------------------------------------------------------------
    # Reserve / settle
    # ------------------------------------------------------------------

    def reserve(self, user: UserRecord, model_id: str,
                est_input_tokens: int, max_output_tokens: int) -> QuotaDecision:
        """Atomically admit a request against the user's daily budgets.

        The condition is evaluated on the counters *before* the ADD, so we
        require `counter <= limit - reservation` (headroom), which makes the
        reserve admission-safe under concurrency.
        """
        if not user.active:
            return QuotaDecision(allowed=False, reason=f"user status is '{user.status}'")

        window = current_window()
        reserve_cost = cost_micro_usd(model_id, est_input_tokens, max_output_tokens)

        # A limit of 0 means "not enforced" for that dimension (see
        # _UNLIMITED_HEADROOM), matching _is_over_budget and the reconciler.
        # Using a large sentinel headroom keeps the dimension non-binding in
        # both the pre-check and the atomic ConditionExpression below.
        cost_headroom = (user.daily_usd_micro - reserve_cost
                         if user.daily_usd_micro else _UNLIMITED_HEADROOM)
        input_headroom = (user.daily_input_tokens - est_input_tokens
                          if user.daily_input_tokens else _UNLIMITED_HEADROOM)
        output_headroom = (user.daily_output_tokens - max_output_tokens
                           if user.daily_output_tokens else _UNLIMITED_HEADROOM)
        if cost_headroom < 0 or input_headroom < 0 or output_headroom < 0:
            self._count_throttle(user.user_id, window)
            return QuotaDecision(
                allowed=False,
                reason="request reservation alone exceeds the daily budget "
                       "(lower max output tokens or raise the user's limits)",
            )

        try:
            resp = self._usage.update_item(
                Key={"user_id": user.user_id, "window": window},
                UpdateExpression=(
                    "ADD cost_micro :c, input_tokens :i, output_tokens :o, requests :one "
                    "SET expires_at = if_not_exists(expires_at, :ttl)"
                ),
                ConditionExpression=(
                    "attribute_not_exists(cost_micro) OR "
                    "(cost_micro <= :ch AND input_tokens <= :ih AND output_tokens <= :oh)"
                ),
                ExpressionAttributeValues={
                    ":c": reserve_cost,
                    ":i": est_input_tokens,
                    ":o": max_output_tokens,
                    ":one": 1,
                    ":ttl": window_ttl_epoch(),
                    ":ch": cost_headroom,
                    ":ih": input_headroom,
                    ":oh": output_headroom,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                self._count_throttle(user.user_id, window)
                return QuotaDecision(allowed=False, reason="daily quota exceeded")
            raise

        new = resp.get("Attributes", {})
        return QuotaDecision(
            allowed=True,
            reservation=Reservation(
                user_id=user.user_id,
                window=window,
                model_id=model_id,
                reserved_cost_micro=reserve_cost,
                reserved_input_tokens=est_input_tokens,
                reserved_output_tokens=max_output_tokens,
            ),
            # None when USD is unlimited (limit 0), else remaining against the
            # single counter (which the reconciler also feeds), so the header
            # reflects native-vended spend too — not just proxy spend.
            remaining_usd_micro=(
                user.daily_usd_micro - int(new.get("cost_micro", 0))
                if user.daily_usd_micro else None
            ),
        )

    def settle(self, reservation: Reservation,
               actual_input_tokens: int | None, actual_output_tokens: int | None,
               cache_write_tokens: int = 0, cache_read_tokens: int = 0,
               failed: bool = False) -> int:
        """Adjust the window counters from the reservation to actual usage.

        Returns the actual cost in micro-USD that was charged. If the
        response carried no usage data, the reservation is kept as the
        charge (conservative). If the upstream call failed, the full
        reservation is released. Cache tokens (Anthropic prompt caching)
        count toward the input-token budget and are priced with the
        configured multipliers.
        """
        if failed:
            actual_input_tokens, actual_output_tokens = 0, 0
            cache_write_tokens = cache_read_tokens = 0
        elif actual_input_tokens is None or actual_output_tokens is None:
            return reservation.reserved_cost_micro  # keep reservation as charge

        actual_cost = cost_micro_usd(
            reservation.model_id, actual_input_tokens, actual_output_tokens,
            cache_write_tokens=cache_write_tokens, cache_read_tokens=cache_read_tokens,
        )
        total_input = actual_input_tokens + cache_write_tokens + cache_read_tokens
        delta_cost = actual_cost - reservation.reserved_cost_micro
        delta_in = total_input - reservation.reserved_input_tokens
        delta_out = actual_output_tokens - reservation.reserved_output_tokens

        expr = "ADD cost_micro :c, input_tokens :i, output_tokens :o"
        values = {":c": delta_cost, ":i": delta_in, ":o": delta_out}
        if failed:
            expr += ", errors :one"
            values[":one"] = 1

        self._usage.update_item(
            Key={"user_id": reservation.user_id, "window": reservation.window},
            UpdateExpression=expr,
            ExpressionAttributeValues=values,
        )
        return actual_cost

    def _count_throttle(self, user_id: str, window: str) -> None:
        self._usage.update_item(
            Key={"user_id": user_id, "window": window},
            UpdateExpression="ADD throttles :one SET expires_at = if_not_exists(expires_at, :ttl)",
            ExpressionAttributeValues={":one": 1, ":ttl": window_ttl_epoch()},
        )

    # ------------------------------------------------------------------
    # Read side (admin API / reconciler)
    # ------------------------------------------------------------------

    def get_window_usage(self, user_id: str, window: str | None = None) -> dict:
        window = window or current_window()
        resp = self._usage.get_item(Key={"user_id": user_id, "window": window})
        item = resp.get("Item") or {}
        # ONE counter set holds the total: the proxy (Mode B) ADDs at settle
        # time and the reconciler ADDs native-vended (Mode A) deltas to the
        # SAME fields (see reconciler _ingest_usage), so no summing is needed
        # and every reader — this, _is_over_budget, and reserve() — agrees.
        return {
            "user_id": user_id,
            "window": window,
            "cost_usd": int(item.get("cost_micro", 0)) / MICRO,
            "input_tokens": int(item.get("input_tokens", 0)),
            "output_tokens": int(item.get("output_tokens", 0)),
            "requests": int(item.get("requests", 0)),
            "throttles": int(item.get("throttles", 0)),
            "errors": int(item.get("errors", 0)),
        }

    def list_users(self) -> list[UserRecord]:
        # Skip SESSION# sentinel items (RoleSessionName -> user_id map).
        def _rows(items):
            return [self._to_user(i) for i in items
                    if not str(i.get("user_id", "")).startswith("SESSION#")]
        users, resp = [], self._users.scan()
        users.extend(_rows(resp.get("Items", [])))
        while "LastEvaluatedKey" in resp:
            resp = self._users.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            users.extend(_rows(resp.get("Items", [])))
        return users
