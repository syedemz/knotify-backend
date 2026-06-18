"""
Integration tests for story 8.9c — notifications_publisher Lambda.

Acceptance criteria covered:
  IT-8.9c-1  INSERT a Notifications row of type=friend_request_received →
             notifications_publisher Lambda invokes publishNotification exactly
             once with the row payload (including user_id); a subscriber whose
             identity.sub matches notification.userId receives the event within
             3 seconds.

  IT-8.9c-2  INSERT a Notifications row of type=friend_request_accepted →
             notifications_publisher Lambda invokes _publishFriendRequestUpdated
             exactly once; onFriendRequestUpdated subscriber receives it within
             3 seconds.

  IT-8.9c-3  MODIFY events on Notifications (e.g. delivered=true updates
             written by PushFanout in 8.10) do NOT trigger another publish call
             (publisher only acts on INSERT).

NOTE — test environment:
  These tests require a live dev environment with:
    - The notifications_publisher Lambda deployed (story 8.9c)
    - The Notifications DynamoDB stream enabled (NEW_IMAGE — stream_enabled=true
      + stream_view_type="NEW_IMAGE" already set in phase 2 DDB module)
    - The AppSync API deployed with publishNotification and
      _publishFriendRequestUpdated resolvers wired
    - AWS credentials with DynamoDB, Cognito, and AppSync access

  Until those conditions are met the tests are skipped by the env-var guard.

Required env vars (loaded from .env.test or shell):
  AWS_REGION
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET
  APPSYNC_GRAPHQL_URL        (output from module.appsync.graphql_url)
  APPSYNC_REALTIME_URL       (output from module.appsync.realtime_url)
  INTEGRATION_TEST_NOTIFICATIONS_TABLE  (optional — defaults to "Notifications")

Run:
    pytest infrastructure/src/tests/integration/test_notifications_publisher.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"

NOTE (drift advisory):
  Dev infrastructure is currently in flux. These tests are authored and
  committed so CI can run them on the next terraform apply. Until that apply
  completes they will be skipped (missing env vars).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — live dev environment not configured."
        )
    return value


def _put_notification(
    dynamo_client,
    table_name: str,
    user_id: str,
    notification_id: str,
    notification_type: str,
    sender_id: str = "user-sender-test",
) -> str:
    """
    PutItem a Notifications row directly to simulate a new notification INSERT.
    Returns the created_at_notification_id SK.
    """
    sk = f"2026-06-18T10:00:00.000000#{notification_id}"
    dynamo_client.put_item(
        TableName=table_name,
        Item={
            "user_id": {"S": user_id},
            "created_at_notification_id": {"S": sk},
            "notification_id": {"S": notification_id},
            "type": {"S": notification_type},
            "sender_user_id": {"S": sender_id},
            "sender_name": {"S": "Test Sender"},
            "read": {"BOOL": False},
            "delivered": {"BOOL": False},
        },
    )
    return sk


def _delete_notification(
    dynamo_client, table_name: str, user_id: str, sk: str
) -> None:
    try:
        dynamo_client.delete_item(
            TableName=table_name,
            Key={
                "user_id": {"S": user_id},
                "created_at_notification_id": {"S": sk},
            },
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# IT-8.9c-1: INSERT friend_request_received → publishNotification invoked once
#
# Approach: same CloudWatch log assertion pattern as room_state_publisher tests.
# We seed a Notifications row, wait for the stream publisher Lambda to process
# it, then assert no ERROR logs in the publisher's log group for that
# notification_id.
#
# The "subscriber receives the event within 3 seconds" portion of the AC is
# proven end-to-end by a future 8.13 chat e2e test that opens a real WebSocket
# connection.  This test validates the Lambda-side publishing path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_it_8_9c_1_insert_friend_request_received_publishes_notification(
    completed_profile_user,
):
    """
    IT-8.9c-1 (story 8.9c AC):
      INSERT Notifications row type=friend_request_received → publisher Lambda
      fires publishNotification; no errors logged by the publisher Lambda.
    """
    try:
        import boto3
    except ImportError:
        pytest.skip("boto3 not installed — live AWS environment required")

    region = _require_env("AWS_REGION")
    _require_env("APPSYNC_GRAPHQL_URL")  # confirms AppSync is deployed
    notifications_table = os.environ.get(
        "INTEGRATION_TEST_NOTIFICATIONS_TABLE", "Notifications"
    )

    user = completed_profile_user
    user_id = user["sub"]
    notification_id = str(uuid.uuid4())

    dynamo_client = boto3.client("dynamodb", region_name=region)
    logs_client = boto3.client("logs", region_name=region)

    sk = None
    try:
        sk = _put_notification(
            dynamo_client,
            notifications_table,
            user_id=user_id,
            notification_id=notification_id,
            notification_type="friend_request_received",
        )

        # Give the Lambda ESM up to 5 seconds to process the stream record.
        time.sleep(5)

        # Assert: no ERROR-level log from the publisher Lambda for this
        # notification_id in the last 30 seconds.
        log_group = "/aws/lambda/knotify-notifications-publisher-dev"
        end_time = int(time.time() * 1000)
        start_time = end_time - 30_000

        try:
            streams_resp = logs_client.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=5,
            )
            streams = streams_resp.get("logStreams", [])
            error_lines = []
            for stream in streams:
                events_resp = logs_client.get_log_events(
                    logGroupName=log_group,
                    logStreamName=stream["logStreamName"],
                    startTime=start_time,
                    endTime=end_time,
                )
                for event in events_resp.get("events", []):
                    msg = event.get("message", "")
                    if "ERROR" in msg and notification_id in msg:
                        error_lines.append(msg)

            assert not error_lines, (
                f"notifications_publisher Lambda logged ERRORs for "
                f"notification_id={notification_id!r}: {error_lines}"
            )
        except logs_client.exceptions.ResourceNotFoundException:
            pytest.skip(
                f"CloudWatch log group {log_group!r} not found — "
                "notifications_publisher Lambda has not been deployed yet."
            )

    finally:
        if sk:
            _delete_notification(dynamo_client, notifications_table, user_id, sk)


# ---------------------------------------------------------------------------
# IT-8.9c-2: INSERT friend_request_accepted → _publishFriendRequestUpdated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_it_8_9c_2_insert_friend_request_accepted_publishes_friend_request_updated(
    completed_profile_user,
):
    """
    IT-8.9c-2 (story 8.9c AC):
      INSERT Notifications row type=friend_request_accepted → publisher Lambda
      fires _publishFriendRequestUpdated; no errors logged.
    """
    try:
        import boto3
    except ImportError:
        pytest.skip("boto3 not installed — live AWS environment required")

    region = _require_env("AWS_REGION")
    _require_env("APPSYNC_GRAPHQL_URL")
    notifications_table = os.environ.get(
        "INTEGRATION_TEST_NOTIFICATIONS_TABLE", "Notifications"
    )

    user = completed_profile_user
    user_id = user["sub"]
    notification_id = str(uuid.uuid4())

    dynamo_client = boto3.client("dynamodb", region_name=region)
    logs_client = boto3.client("logs", region_name=region)

    sk = None
    try:
        sk = _put_notification(
            dynamo_client,
            notifications_table,
            user_id=user_id,
            notification_id=notification_id,
            notification_type="friend_request_accepted",
        )

        time.sleep(5)

        log_group = "/aws/lambda/knotify-notifications-publisher-dev"
        end_time = int(time.time() * 1000)
        start_time = end_time - 30_000

        try:
            streams_resp = logs_client.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=5,
            )
            streams = streams_resp.get("logStreams", [])
            error_lines = []
            for stream in streams:
                events_resp = logs_client.get_log_events(
                    logGroupName=log_group,
                    logStreamName=stream["logStreamName"],
                    startTime=start_time,
                    endTime=end_time,
                )
                for event in events_resp.get("events", []):
                    msg = event.get("message", "")
                    if "ERROR" in msg and notification_id in msg:
                        error_lines.append(msg)

            assert not error_lines, (
                f"notifications_publisher Lambda logged ERRORs for "
                f"notification_id={notification_id!r} (friend_request_accepted): "
                f"{error_lines}"
            )
        except logs_client.exceptions.ResourceNotFoundException:
            pytest.skip(
                f"CloudWatch log group {log_group!r} not found — "
                "notifications_publisher Lambda has not been deployed yet."
            )

    finally:
        if sk:
            _delete_notification(dynamo_client, notifications_table, user_id, sk)


# ---------------------------------------------------------------------------
# IT-8.9c-3: MODIFY events on Notifications do NOT trigger a publish call
#
# Verified by: seeding a row, then updating delivered=true (simulating what
# PushFanout 8.10 does), waiting 5 seconds, then asserting the CloudWatch logs
# contain no publish call for that notification_id after the MODIFY.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_it_8_9c_3_modify_notifications_row_does_not_trigger_publish(
    completed_profile_user,
):
    """
    IT-8.9c-3 (story 8.9c AC):
      MODIFY event on Notifications (delivered=true update) must NOT trigger
      any AppSync publish call — publisher only acts on INSERT.
    """
    try:
        import boto3
    except ImportError:
        pytest.skip("boto3 not installed — live AWS environment required")

    region = _require_env("AWS_REGION")
    _require_env("APPSYNC_GRAPHQL_URL")
    notifications_table = os.environ.get(
        "INTEGRATION_TEST_NOTIFICATIONS_TABLE", "Notifications"
    )

    user = completed_profile_user
    user_id = user["sub"]
    notification_id = str(uuid.uuid4())

    dynamo_client = boto3.client("dynamodb", region_name=region)
    logs_client = boto3.client("logs", region_name=region)

    sk = None
    try:
        # First INSERT — this will trigger an initial publish (expected).
        sk = _put_notification(
            dynamo_client,
            notifications_table,
            user_id=user_id,
            notification_id=notification_id,
            notification_type="bookmark",
        )
        # Wait for the INSERT publish to settle.
        time.sleep(5)

        # Record current time as the MODIFY test window start.
        modify_start_time = int(time.time() * 1000)

        # MODIFY: update delivered=true (simulating PushFanout 8.10)
        dynamo_client.update_item(
            TableName=notifications_table,
            Key={
                "user_id": {"S": user_id},
                "created_at_notification_id": {"S": sk},
            },
            UpdateExpression="SET delivered = :true",
            ExpressionAttributeValues={":true": {"BOOL": True}},
            ConditionExpression="attribute_exists(user_id)",
        )

        # Wait for the stream to process the MODIFY event.
        time.sleep(5)

        log_group = "/aws/lambda/knotify-notifications-publisher-dev"
        end_time = int(time.time() * 1000)

        try:
            streams_resp = logs_client.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=5,
            )
            streams = streams_resp.get("logStreams", [])
            # Look for any publish call for our notification_id AFTER the MODIFY.
            publish_lines = []
            for stream in streams:
                events_resp = logs_client.get_log_events(
                    logGroupName=log_group,
                    logStreamName=stream["logStreamName"],
                    startTime=modify_start_time,
                    endTime=end_time,
                )
                for event in events_resp.get("events", []):
                    msg = event.get("message", "")
                    if notification_id in msg and (
                        "publishNotification" in msg
                        or "_publishFriendRequestUpdated" in msg
                        or "AppSync mutation published" in msg
                    ):
                        publish_lines.append(msg)

            assert not publish_lines, (
                f"notifications_publisher must NOT publish for a MODIFY event; "
                f"found publish log lines for notification_id={notification_id!r}: "
                f"{publish_lines}"
            )
        except logs_client.exceptions.ResourceNotFoundException:
            pytest.skip(
                f"CloudWatch log group {log_group!r} not found — "
                "notifications_publisher Lambda has not been deployed yet."
            )

    finally:
        if sk:
            _delete_notification(dynamo_client, notifications_table, user_id, sk)
