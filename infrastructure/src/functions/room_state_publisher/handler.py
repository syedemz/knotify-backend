"""
room_state_publisher Lambda — story 8.9a

Consumes a DynamoDB Stream on the ChatRooms table and publishes AppSync
mutations when room status transitions occur:
  - active   → deactivated : calls _publishRoomDeactivated(roomId, payload)
  - deactivated → active   : calls _publishRoomReactivated(roomId, payload)

VPC placement: OUTSIDE the VPC.
WHY: AppSync HTTPS endpoints are reachable via public DNS; no VPC endpoint
is needed.  Running outside the VPC avoids the ENI attachment cold-start
penalty and prevents the blackhole failure documented in hotfix #106 (private
subnets with no NAT egress cannot reach public service endpoints).

SigV4 auth against AppSync:
  AppSync's secondary auth mode is AWS_IAM; the publish mutations carry
  @aws_iam.  This Lambda's execution role (room_state_publisher_role) has
  appsync:GraphQL scoped to the exact publish-mutation field ARNs.  We sign
  the GraphQL POST with botocore SigV4 helpers rather than a boto3 client
  (boto3 has no AppSync GraphQL client).
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
# GraphQL mutation bodies for the two room-state transitions
# ---------------------------------------------------------------------------

_PUBLISH_ROOM_DEACTIVATED_MUTATION = """
mutation PublishRoomDeactivated($roomId: ID!, $payload: AWSJSON!) {
  _publishRoomDeactivated(roomId: $roomId, payload: $payload) {
    roomId
    status
  }
}
"""

_PUBLISH_ROOM_REACTIVATED_MUTATION = """
mutation PublishRoomReactivated($roomId: ID!, $payload: AWSJSON!) {
  _publishRoomReactivated(roomId: $roomId, payload: $payload) {
    roomId
    status
  }
}
"""

_MUTATION_BODIES: dict[str, str] = {
    "_publishRoomDeactivated": _PUBLISH_ROOM_DEACTIVATED_MUTATION,
    "_publishRoomReactivated": _PUBLISH_ROOM_REACTIVATED_MUTATION,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> None:
    """Lambda handler: iterate stream records and publish on status transitions."""
    records = event.get("Records", [])
    logger.info(
        "room_state_publisher invoked",
        extra={"record_count": len(records)},
    )
    for record in records:
        _process_record(record)


# ---------------------------------------------------------------------------
# Record processing
# ---------------------------------------------------------------------------


def _process_record(record: dict) -> None:
    """Inspect a single DynamoDB stream record and publish if status changed."""
    event_name: str = record.get("eventName", "")
    if event_name != "MODIFY":
        # INSERT (room creation) and REMOVE (row deletion) are not status transitions.
        logger.debug(
            "Skipping non-MODIFY record",
            extra={"event_name": event_name},
        )
        return

    dynamodb = record.get("dynamodb", {})
    old_image = dynamodb.get("OldImage")
    new_image = dynamodb.get("NewImage")

    if not old_image or not new_image:
        # Missing images (e.g. partial stream config) — cannot determine transition.
        logger.warning(
            "MODIFY record missing OldImage or NewImage — skipping",
            extra={"has_old": old_image is not None, "has_new": new_image is not None},
        )
        return

    old_status = _extract_str(old_image, "status")
    new_status = _extract_str(new_image, "status")

    if old_status == new_status:
        # Status unchanged (e.g. last_message_at update) — do not publish.
        logger.debug(
            "Status unchanged — skipping publish",
            extra={"status": new_status},
        )
        return

    room_id = _extract_str(new_image, "room_id")
    payload = _build_payload(new_image)

    if old_status == "active" and new_status == "deactivated":
        logger.info(
            "Room status active→deactivated — publishing _publishRoomDeactivated",
            extra={"room_id": room_id},
        )
        _publish_to_appsync("_publishRoomDeactivated", room_id, payload)
    elif old_status == "deactivated" and new_status == "active":
        logger.info(
            "Room status deactivated→active — publishing _publishRoomReactivated",
            extra={"room_id": room_id},
        )
        _publish_to_appsync("_publishRoomReactivated", room_id, payload)
    else:
        logger.warning(
            "Unrecognised status transition — skipping",
            extra={"old_status": old_status, "new_status": new_status},
        )


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def _build_payload(new_image: dict) -> dict:
    """
    Convert a DynamoDB NewImage dict into a plain Python dict with camelCase
    keys matching the GraphQL ChatRoom type fields in schema.graphql.

    Only fields present in the image are included (optional fields absent from
    the image are set to None so callers can check `if payload["key"]`).
    """
    payload: dict[str, Any] = {
        "roomId": _extract_str(new_image, "room_id"),
        "userA": _extract_str(new_image, "user_a"),
        "userB": _extract_str(new_image, "user_b"),
        "status": _extract_str(new_image, "status"),
        "friendshipActive": _extract_bool(new_image, "friendship_active"),
        # Optional fields — may be absent from the image
        "deactivatedReason": _extract_str(new_image, "deactivated_reason"),
        "deactivatedBy": _extract_str(new_image, "deactivated_by"),
        "deactivatedAt": _extract_str(new_image, "deactivated_at"),
        "reactivatedAt": _extract_str(new_image, "reactivated_at"),
    }
    return payload


# ---------------------------------------------------------------------------
# AppSync SigV4 publish
# ---------------------------------------------------------------------------


def _publish_to_appsync(mutation_name: str, room_id: str, payload: dict) -> None:
    """
    POST a GraphQL mutation to AppSync using SigV4 (IAM auth mode).

    Uses botocore's SigV4Auth to sign the request — boto3 has no AppSync
    GraphQL client, so we construct the signed request manually.
    """
    url = APPSYNC_GRAPHQL_URL
    if not url:
        logger.error("APPSYNC_GRAPHQL_URL is not set — cannot publish")
        return

    mutation_body = _MUTATION_BODIES[mutation_name]
    request_body = json.dumps(
        {
            "query": mutation_body,
            "variables": {
                "roomId": room_id,
                "payload": json.dumps(payload),
            },
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
                "room_id": room_id,
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
                "room_id": room_id,
                "errors": body["errors"],
            },
        )
        raise RuntimeError(
            f"AppSync mutation {mutation_name} returned errors: {body['errors']}"
        )

    logger.info(
        "AppSync mutation published successfully",
        extra={"mutation": mutation_name, "room_id": room_id},
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
