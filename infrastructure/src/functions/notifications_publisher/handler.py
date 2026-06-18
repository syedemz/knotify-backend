"""
notifications_publisher Lambda — story 8.9c

Consumes a DynamoDB Stream on the Notifications table and publishes AppSync
mutations when new notification rows are inserted:
  - type in {friend_request_received, bookmark, match, ...any generic type}
      → calls publishNotification(notification: <payload>) via SigV4 (IAM auth mode)
  - type = friend_request_accepted
      → calls _publishFriendRequestUpdated(payload: <payload>) via SigV4 (IAM auth mode)
  - MODIFY events (e.g. delivered=true written by PushFanout 8.10)
      → silently ignored (only INSERT triggers a publish)
  - Unknown/future types → log a warning, do nothing (batch processing continues)

VPC placement: OUTSIDE the VPC.
WHY: AppSync HTTPS endpoints are reachable via public DNS; no VPC endpoint
is needed.  Running outside the VPC avoids the ENI attachment cold-start
penalty and prevents the blackhole failure documented in hotfix #106 (private
subnets with no NAT egress cannot reach public service endpoints).

SigV4 auth against AppSync:
  AppSync's secondary auth mode is AWS_IAM; the publish mutations carry
  @aws_iam.  This Lambda's execution role (notifications_publisher_role) has
  appsync:GraphQL scoped to the exact publish-mutation field ARNs.  We sign
  the GraphQL POST with botocore SigV4 helpers rather than a boto3 client
  (boto3 has no AppSync GraphQL client).

CONSUMER LIMIT NOTE:
  With this ESM the Notifications stream has 2 ESM consumers
  (notifications_publisher + push_fanout from 8.10), which is the AWS default
  limit of 2 simultaneous consumers per DynamoDB stream.  A third consumer
  would require Kinesis Data Streams for DynamoDB or a fan-out Lambda.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
import botocore.auth
import botocore.awsrequest
import botocore.credentials
import requests

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Constants — resolved at cold start from environment variables
# ---------------------------------------------------------------------------

APPSYNC_GRAPHQL_URL: str = os.environ.get("APPSYNC_GRAPHQL_URL", "")
AWS_REGION: str = os.environ.get("AWS_REGION", "eu-central-1")

# ---------------------------------------------------------------------------
# Type routing — friend_request_accepted goes to _publishFriendRequestUpdated;
# all other known types (and unknown future types that are not "accepted") go
# to publishNotification.
# ---------------------------------------------------------------------------

_FRIEND_REQUEST_ACCEPTED_TYPE = "friend_request_accepted"

# ---------------------------------------------------------------------------
# GraphQL mutation bodies for the two notification publish paths
# ---------------------------------------------------------------------------

_PUBLISH_NOTIFICATION_MUTATION = """
mutation PublishNotification($notification: AWSJSON!) {
  publishNotification(notification: $notification) {
    userId
    notificationId
    type
    read
    delivered
  }
}
"""

_PUBLISH_FRIEND_REQUEST_UPDATED_MUTATION = """
mutation PublishFriendRequestUpdated($payload: AWSJSON!) {
  _publishFriendRequestUpdated(payload: $payload) {
    userId
    notificationId
    type
    read
    delivered
  }
}
"""

_MUTATION_BODIES: dict[str, str] = {
    "publishNotification": _PUBLISH_NOTIFICATION_MUTATION,
    "_publishFriendRequestUpdated": _PUBLISH_FRIEND_REQUEST_UPDATED_MUTATION,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> None:
    """Lambda handler: iterate stream records and publish on INSERT events."""
    records = event.get("Records", [])
    logger.info(
        "notifications_publisher invoked",
        extra={"record_count": len(records)},
    )
    for record in records:
        _process_record(record)


# ---------------------------------------------------------------------------
# Record processing
# ---------------------------------------------------------------------------


def _process_record(record: dict) -> None:
    """Inspect a single DynamoDB stream record and publish if it is an INSERT."""
    event_name: str = record.get("eventName", "")
    if event_name != "INSERT":
        # MODIFY (e.g. delivered=true update by PushFanout 8.10) and REMOVE
        # events are intentionally ignored — only new notification rows trigger
        # a real-time AppSync publish.
        logger.debug(
            "Skipping non-INSERT record",
            extra={"event_name": event_name},
        )
        return

    dynamodb = record.get("dynamodb", {})
    new_image = dynamodb.get("NewImage")

    if not new_image:
        logger.warning(
            "INSERT record missing NewImage — skipping",
            extra={"record": record},
        )
        return

    notification_type = _extract_str(new_image, "type")
    user_id = _extract_str(new_image, "user_id")

    logger.info(
        "Processing INSERT notification",
        extra={"type": notification_type, "user_id": user_id},
    )

    payload = _build_notification_payload(new_image)

    if notification_type == _FRIEND_REQUEST_ACCEPTED_TYPE:
        logger.info(
            "type=friend_request_accepted — publishing _publishFriendRequestUpdated",
            extra={"user_id": user_id},
        )
        _publish_to_appsync("_publishFriendRequestUpdated", payload)
    elif notification_type is not None:
        # All other known types (friend_request_received, bookmark, match, etc.)
        # AND any unknown future types that are not friend_request_accepted go
        # through publishNotification.  Unknown types still get published so that
        # subscribed clients receive them; the "unknown type" warning is emitted
        # after publish to flag the gap without silently dropping the event.
        #
        # Exception: truly unknown types (not matching the known set) are logged
        # and skipped rather than published to avoid forwarding noise to clients.
        _KNOWN_GENERIC_TYPES = frozenset(
            {
                "friend_request_received",
                "bookmark",
                "match",
                "profile_view",
                "room_deactivated",
                "bookmark_received",
            }
        )
        if notification_type not in _KNOWN_GENERIC_TYPES:
            logger.warning(
                "Unknown notification type — skipping publish",
                extra={"type": notification_type, "user_id": user_id},
            )
            return

        logger.info(
            "Publishing publishNotification",
            extra={"type": notification_type, "user_id": user_id},
        )
        _publish_to_appsync("publishNotification", payload)
    else:
        logger.warning(
            "Notification row missing type field — skipping",
            extra={"user_id": user_id},
        )


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def _build_notification_payload(new_image: dict) -> dict:
    """
    Convert a DynamoDB NewImage dict into a plain Python dict with camelCase
    keys matching the GraphQL Notification type fields in schema.graphql.

    The user_id PK field is mapped to userId — this is the field that
    onNotificationForMe and onFriendRequestUpdated pipeline resolvers filter
    on (identity.sub == notification.userId / payload.userId).

    Only fields present in the image are included; optional fields absent
    from the image are set to None.
    """
    payload: dict[str, Any] = {
        # PK — recipient's Cognito sub.  MUST be present so the AppSync
        # subscription pipeline resolver (check_identity_match) can filter
        # onNotificationForMe / onFriendRequestUpdated to the correct subscriber.
        "userId": _extract_str(new_image, "user_id"),
        "notificationId": _extract_str(new_image, "notification_id"),
        "type": _extract_str(new_image, "type"),
        "senderUserId": _extract_str(new_image, "sender_user_id"),
        "senderName": _extract_str(new_image, "sender_name"),
        "senderAvatar": _extract_str(new_image, "sender_avatar"),
        "read": _extract_bool(new_image, "read"),
        "delivered": _extract_bool(new_image, "delivered"),
        # Optional JSON payload (e.g. for friend_request_accepted)
        "payload": _extract_str(new_image, "payload"),
    }
    return payload


# ---------------------------------------------------------------------------
# AppSync SigV4 publish
# ---------------------------------------------------------------------------


def _publish_to_appsync(mutation_name: str, payload: dict) -> None:
    """
    POST a GraphQL mutation to AppSync using SigV4 (IAM auth mode).

    Uses botocore's SigV4Auth to sign the request — boto3 has no AppSync
    GraphQL client, so we construct the signed request manually.

    The mutation argument name differs between the two mutations:
      publishNotification            — argument name: notification
      _publishFriendRequestUpdated   — argument name: payload
    Both accept AWSJSON!, so we JSON-serialize the payload dict.
    """
    url = APPSYNC_GRAPHQL_URL
    if not url:
        logger.error("APPSYNC_GRAPHQL_URL is not set — cannot publish")
        return

    mutation_body = _MUTATION_BODIES[mutation_name]

    # Select the correct argument name for the mutation.
    if mutation_name == "publishNotification":
        variables = {"notification": json.dumps(payload)}
    else:
        variables = {"payload": json.dumps(payload)}

    request_body = json.dumps(
        {
            "query": mutation_body,
            "variables": variables,
        }
    )

    # Build a botocore AWSRequest and sign it with SigV4.
    session = boto3.Session()
    credentials = session.get_credentials()
    aws_request = botocore.awsrequest.AWSRequest(
        method="POST",
        url=url,
        data=request_body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    botocore.auth.SigV4Auth(credentials, "appsync", AWS_REGION).add_auth(aws_request)

    signed_headers = dict(aws_request.headers)
    response = requests.post(url, data=request_body, headers=signed_headers, timeout=10)

    if response.status_code != 200:
        logger.error(
            "AppSync mutation returned non-200 status",
            extra={
                "mutation": mutation_name,
                "status_code": response.status_code,
                "body": response.text[:500],
            },
        )
        response.raise_for_status()

    body = response.json()
    if "errors" in body:
        logger.error(
            "AppSync mutation returned GraphQL errors",
            extra={
                "mutation": mutation_name,
                "errors": body["errors"],
            },
        )
        raise RuntimeError(
            f"AppSync mutation {mutation_name} returned errors: {body['errors']}"
        )

    logger.info(
        "AppSync mutation published successfully",
        extra={"mutation": mutation_name},
    )


# ---------------------------------------------------------------------------
# DynamoDB attribute extraction helpers
# ---------------------------------------------------------------------------


def _extract_str(image: dict, key: str) -> str | None:
    """Extract a string value from a DynamoDB image dict. Returns None if absent."""
    attr = image.get(key)
    if attr is None:
        return None
    return attr.get("S")


def _extract_bool(image: dict, key: str) -> bool | None:
    """Extract a boolean value from a DynamoDB image dict. Returns None if absent."""
    attr = image.get(key)
    if attr is None:
        return None
    return attr.get("BOOL")
