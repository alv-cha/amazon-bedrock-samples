"""Ingest per-user Bedrock usage from model-invocation logs.

This is what makes enforcement API- and provider-agnostic: instead of
proxying inference, we read Amazon Bedrock's own model-invocation logging,
which records input/output token counts for EVERY invoke path
(InvokeModel, Converse, streaming, all providers) in a uniform shape.

Attribution to an end-user works because the broker vended credentials
with RoleSessionName = a sanitized/hashed form of the JWT `sub`. That
session name appears inside the assumed-role ARN in each log record's
`identity.arn`. We aggregate tokens per session name over the current UTC
window, reverse-map the session name to the real user id (via
QuotaStore.resolve_session), price it, and upsert the usage row the
block/unblock logic already reads.

CloudWatch Logs Insights is used for the aggregation so we push the
grouping/sum server-side and only pull one row per session.
"""

import re
import time
from datetime import datetime, timezone

# assumed-role ARN:
#   arn:aws:sts::<acct>:assumed-role/<RoleName>/<RoleSessionName>
_SESSION_FROM_ARN = re.compile(r"assumed-role/[^/]+/(?P<session>[\w+=,.@-]+)$")

# Model-invocation log records carry token counts under `input`/`output`
# with slightly different key names across the invoke paths; Insights lets
# us coalesce them. `modelId` is present on every record.
_INSIGHTS_QUERY = """
fields identity.arn as arn, modelId as model,
       coalesce(input.inputTokenCount, input.inputBodyTokenCount, 0) as in_tok,
       coalesce(output.outputTokenCount, output.outputBodyTokenCount, 0) as out_tok
| filter ispresent(identity.arn)
| stats sum(in_tok) as input_tokens,
        sum(out_tok) as output_tokens,
        count(*) as requests by arn, model
"""


def _window_epoch_bounds(now: datetime) -> tuple[int, int]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _session_from_arn(arn: str) -> str | None:
    m = _SESSION_FROM_ARN.search(arn or "")
    return m.group("session") if m else None


def run_insights_query(logs_client, log_group: str, start: int, end: int,
                       poll_seconds: float = 1.0, timeout_seconds: float = 50.0) -> list[dict]:
    """Run the aggregation query and return rows as {field: value} dicts."""
    started = logs_client.start_query(
        logGroupName=log_group,
        startTime=start,
        endTime=end,
        queryString=_INSIGHTS_QUERY,
    )
    query_id = started["queryId"]
    deadline = time.monotonic() + timeout_seconds
    while True:
        resp = logs_client.get_query_results(queryId=query_id)
        status = resp.get("status")
        if status == "Complete":
            rows = []
            for row in resp.get("results", []):
                rows.append({c["field"]: c["value"] for c in row})
            return rows
        if status in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Logs Insights query {status.lower()}")
        if time.monotonic() > deadline:
            logs_client.stop_query(queryId=query_id)
            raise RuntimeError("Logs Insights query timed out")
        time.sleep(poll_seconds)


def aggregate_by_user(rows: list[dict], store) -> dict[str, list[dict]]:
    """Group per-(session,model) Insights rows into per-user_id buckets.

    Returns { user_id: [ {model, input_tokens, output_tokens, requests}, ... ] }.
    Rows whose session name can't be reversed to a known user are dropped
    (with the session name collected by the caller for observability).
    """
    per_user: dict[str, list[dict]] = {}
    unresolved: set[str] = set()
    session_to_user: dict[str, str | None] = {}

    for r in rows:
        session = _session_from_arn(r.get("arn", ""))
        if not session:
            continue
        if session not in session_to_user:
            session_to_user[session] = store.resolve_session(session)
        user_id = session_to_user[session]
        if not user_id:
            unresolved.add(session)
            continue
        per_user.setdefault(user_id, []).append({
            "model": r.get("model", "") or "unknown",
            "input_tokens": int(float(r.get("input_tokens", 0) or 0)),
            "output_tokens": int(float(r.get("output_tokens", 0) or 0)),
            "requests": int(float(r.get("requests", 0) or 0)),
        })
    aggregate_by_user.last_unresolved = sorted(unresolved)  # type: ignore[attr-defined]
    return per_user
