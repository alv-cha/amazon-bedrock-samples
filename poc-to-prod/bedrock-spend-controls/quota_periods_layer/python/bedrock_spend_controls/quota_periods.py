"""Pure UTC calendar-period and quota evaluation helpers.

Daily DynamoDB rows remain the canonical usage ledger. Weekly and monthly
totals are derived from those retained rows so enabling a longer-period limit
mid-period includes usage that occurred before the limit was enabled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Mapping


PERIODS = ("daily", "weekly", "monthly")
DIMENSIONS = ("usd", "input_tokens", "output_tokens")
USAGE_FIELDS = {
    "usd": "cost_micro",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
}
_DAILY_WINDOW = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class CalendarWindow:
    period: str
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return self.start.date().isoformat()


@dataclass(frozen=True)
class QuotaBreach:
    period: str
    dimension: str
    usage: int
    limit: int
    window: CalendarWindow


@dataclass(frozen=True)
class QuotaEvaluation:
    breaches: tuple[QuotaBreach, ...]
    ratios: dict[str, float]

    @property
    def over_budget(self) -> bool:
        return bool(self.breaches)

    @property
    def maximum_ratio(self) -> float:
        return max(self.ratios.values(), default=0.0)


def as_utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def calendar_window(period: str, value: datetime | None = None) -> CalendarWindow:
    current = as_utc(value)
    current_date = current.date()
    if period == "daily":
        start_date = current_date
        end_date = start_date + timedelta(days=1)
    elif period == "weekly":
        start_date = current_date - timedelta(days=current_date.weekday())
        end_date = start_date + timedelta(days=7)
    elif period == "monthly":
        start_date = current_date.replace(day=1)
        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(start_date.year, start_date.month + 1, 1)
    else:
        raise ValueError(f"unsupported quota period: {period}")
    return CalendarWindow(
        period=period,
        start=datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        end=datetime.combine(end_date, time.min, tzinfo=timezone.utc),
    )


def calendar_windows(value: datetime | None = None) -> dict[str, CalendarWindow]:
    current = as_utc(value)
    return {period: calendar_window(period, current) for period in PERIODS}


def period_for_start(period: str, start: str) -> CalendarWindow:
    try:
        parsed = date.fromisoformat(start)
    except (TypeError, ValueError) as exc:
        raise ValueError("window must be an ISO date") from exc
    window = calendar_window(
        period, datetime.combine(parsed, time.min, tzinfo=timezone.utc)
    )
    if window.start.date() != parsed:
        raise ValueError(f"window is not a {period} period start")
    return window


def aggregate_daily_rows(
    rows: Iterable[Mapping[str, object]],
    value: datetime | None = None,
) -> dict[str, dict[str, object]]:
    windows = calendar_windows(value)
    totals: dict[str, dict[str, object]] = {
        period: {
            "period": period,
            "window": window.key,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "resets_at": window.end.isoformat(),
            "cost_micro": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 0,
        }
        for period, window in windows.items()
    }
    for row in rows:
        raw_window = str(row.get("window", ""))
        if not _DAILY_WINDOW.fullmatch(raw_window):
            continue
        try:
            row_date = date.fromisoformat(raw_window)
        except ValueError:
            continue
        for period, window in windows.items():
            if window.start.date() <= row_date < window.end.date():
                target = totals[period]
                for field in (
                    "cost_micro",
                    "input_tokens",
                    "output_tokens",
                    "requests",
                ):
                    target[field] = int(target[field]) + int(row.get(field, 0))
    return totals


def limits_from_item(item: Mapping[str, object]) -> dict[str, dict[str, int] | None]:
    result: dict[str, dict[str, int] | None] = {}
    for period in PERIODS:
        enabled_default = period == "daily"
        enabled = bool(item.get(f"{period}_limits_enabled", enabled_default))
        if not enabled:
            result[period] = None
            continue
        result[period] = {
            "usd_micro": int(item.get(f"{period}_usd_micro", 0)),
            "input_tokens": int(item.get(f"{period}_input_tokens", 0)),
            "output_tokens": int(item.get(f"{period}_output_tokens", 0)),
        }
    return result


def evaluate_limits(
    limits: Mapping[str, Mapping[str, int] | None],
    usage: Mapping[str, Mapping[str, object]],
    value: datetime | None = None,
) -> QuotaEvaluation:
    windows = calendar_windows(value)
    breaches: list[QuotaBreach] = []
    ratios: dict[str, float] = {}
    for period in PERIODS:
        period_limits = limits.get(period)
        if period_limits is None:
            continue
        period_usage = usage.get(period, {})
        for dimension in DIMENSIONS:
            limit_key = "usd_micro" if dimension == "usd" else dimension
            limit = int(period_limits.get(limit_key, 0))
            if limit <= 0:
                continue
            current = int(period_usage.get(USAGE_FIELDS[dimension], 0))
            ratios[f"{period}.{dimension}"] = current / limit
            if current >= limit:
                breaches.append(
                    QuotaBreach(
                        period=period,
                        dimension=dimension,
                        usage=current,
                        limit=limit,
                        window=windows[period],
                    )
                )
    breaches.sort(
        key=lambda item: (
            PERIODS.index(item.period),
            DIMENSIONS.index(item.dimension),
        )
    )
    return QuotaEvaluation(tuple(breaches), ratios)


def quota_reason(evaluation: QuotaEvaluation) -> str:
    if not evaluation.breaches:
        return ""
    first = evaluation.breaches[0]
    dimension = (
        "USD"
        if first.dimension == "usd"
        else first.dimension.replace("_", " ")
    )
    return (
        f"auto: {first.period} {dimension} quota exhausted "
        f"in {first.window.key}"
    )
