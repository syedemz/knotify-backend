"""
knotify-stale-token-cleanup Lambda handler.

Scheduled daily via an EventBridge (CloudWatch Events) rule.

Scans the PushNotificationTokens DynamoDB table and deletes every row whose
last_seen timestamp is strictly older than 60 days from now.

Design notes:
  - OUTSIDE the VPC: only touches DynamoDB (no VPC endpoint or NAT required).
    Consistent with hotfix #106 lesson — private subnets without NAT cannot
    reach public service endpoints.
  - last_seen is an ISO 8601 UTC string written by the push_tokens Lambda
    (story 8.11).  ISO-8601 strings sort lexically, so we compare the raw
    string against the cutoff ISO string without parsing every row.
  - Deletion threshold: strictly older than 60 days (last_seen < cutoff).
  - Scan pagination: LastEvaluatedKey loop exhausts all pages.
  - Per-item DeleteItem: table is small in dev; for a production table with
    millions of rows BatchWriteItem would be more efficient, but the simplest
    correct implementation is chosen here (engineering principles: KISS, no
    premature optimisation).
  - 60-day threshold is hardcoded — no configuration knob per story notes.
  - No metrics emission beyond standard CloudWatch logs.

Module-level singletons:
  _dynamo: boto3 DynamoDB client cached across warm Lambda invocations.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_PUSH_TOKENS: str = os.environ.get("TABLE_PUSH_TOKENS", "PushNotificationTokens")

# Stale threshold: rows whose last_seen is strictly older than this many days
# are deleted.  Hardcoded per story 8.12 notes — no configurable knob.
_STALE_DAYS: int = 60

# ---------------------------------------------------------------------------
# Module-level singleton (cold-start optimisation)
# ---------------------------------------------------------------------------

_dynamo = None


def _get_dynamo():
    """Return a (possibly cached) boto3 DynamoDB client."""
    global _dynamo
    if _dynamo is None:
        import boto3  # deferred — not available in unit-test environments
        _dynamo = boto3.client("dynamodb")
    return _dynamo


# ---------------------------------------------------------------------------
# Core scan-and-delete logic
# ---------------------------------------------------------------------------


def _cutoff_iso() -> str:
    """
    Return an ISO 8601 UTC string representing 60 days ago.

    Items whose last_seen is lexically less than this string are stale.
    ISO-8601 strings with the same UTC offset sort chronologically, so the
    lexical comparison is correct for UTC-normalised timestamps.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)
    return cutoff.isoformat()


def _scan_all_items(client: Any, table: str) -> list[dict]:
    """
    Exhaust all Scan pages for *table* and return every item.

    Uses LastEvaluatedKey pagination so large tables are handled correctly.
    """
    items: list[dict] = []
    kwargs: dict = {
        "TableName": table,
        "ProjectionExpression": "user_id, device_id, last_seen",
    }

    while True:
        response = client.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items


def _is_stale(item: dict, cutoff: str) -> bool:
    """
    Return True if the item's last_seen timestamp is strictly older than
    the given cutoff ISO string.

    last_seen is stored as {"S": "<iso-string>"}.  If the attribute is absent
    (data anomaly) the item is treated as stale and deleted.
    """
    attr = item.get("last_seen")
    if attr is None:
        logger.warning(
            "item missing last_seen attribute — treating as stale",
            extra={
                "user_id": item.get("user_id", {}).get("S"),
                "device_id": item.get("device_id", {}).get("S"),
            },
        )
        return True
    last_seen_str: str = attr.get("S", "")
    return last_seen_str < cutoff


def _delete_token(client: Any, table: str, user_id: str, device_id: str) -> None:
    """Delete a single PushNotificationTokens row by its composite key."""
    client.delete_item(
        TableName=table,
        Key={
            "user_id":   {"S": user_id},
            "device_id": {"S": device_id},
        },
    )


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


def handler(event: dict, context: object) -> None:
    """
    knotify-stale-token-cleanup Lambda entrypoint.

    Invoked daily by an EventBridge scheduled rule.  Scans the
    PushNotificationTokens table and deletes every row whose last_seen
    is strictly older than 60 days.

    Args:
        event:   EventBridge scheduled event (unused — no payload consumed).
        context: Lambda context object (unused).

    Returns:
        None — EventBridge scheduled Lambdas do not return a meaningful value.
    """
    client = _get_dynamo()
    cutoff = _cutoff_iso()
    table = _TABLE_PUSH_TOKENS

    logger.info(
        "stale_token_cleanup started",
        extra={"table": table, "cutoff": cutoff, "stale_days": _STALE_DAYS},
    )

    items = _scan_all_items(client, table)

    deleted = 0
    skipped = 0

    for item in items:
        user_id = item.get("user_id", {}).get("S", "")
        device_id = item.get("device_id", {}).get("S", "")

        if _is_stale(item, cutoff):
            _delete_token(client, table, user_id, device_id)
            deleted += 1
            logger.debug(
                "deleted stale token",
                extra={"user_id": user_id, "device_id": device_id},
            )
        else:
            skipped += 1

    logger.info(
        "stale_token_cleanup finished",
        extra={"deleted": deleted, "skipped": skipped},
    )
