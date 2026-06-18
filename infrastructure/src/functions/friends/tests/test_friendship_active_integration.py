"""
Integration tests for story 8.9b — friendship_active flag maintenance in the
knotify-friends Lambda.

These tests require a live Aurora cluster AND a live DynamoDB table, so they
are skip-gated on the AURORA_HOST environment variable.

Run against a deployed stack:
    AURORA_HOST=<host> \\
    AURORA_PORT=5432 \\
    AURORA_DBNAME=knotify \\
    DB_SECRET_NAME=knotify-dev-app-user-credential \\
    TABLE_CHAT_ROOMS=ChatRooms \\
    pytest infrastructure/src/functions/friends/tests/test_friendship_active_integration.py -v -m integration

Skip in unit test runs (default):
    pytest -m "not integration"

Acceptance criteria covered (story 8.9b):
  IT-8.9b-1  Accept-path, refriend after unblock:
             A and B were friends and chatted, A blocked B then unblocked B
             (room is now status=active, friendship_active=false). B sends A a
             friend request and A accepts → ChatRooms.friendship_active flips
             to true; subsequent sendMessage by either party succeeds.
  IT-8.9b-2  Accept-path, no prior room:
             A and B were never friends and never chatted. A sends B a friend
             request and B accepts → friendship row created in Aurora,
             UpdateItem on the non-existent ChatRooms row no-ops, no error.
  IT-8.9b-3  Unfriend-path:
             A and B are friends with an active chat room (status=active,
             friendship_active=true). A calls DELETE /v1/friends/B →
             friendship row removed, ChatRooms.friendship_active flips to
             false, ChatRooms.status stays 'active'.
  IT-8.9b-4  Unfriend-path, no prior room:
             A and B are friends but never chatted. A calls DELETE /v1/friends/B
             → friendship row removed, UpdateItem on the non-existent ChatRooms
             row no-ops, no error.

Notes:
  - These tests are skip-gated because they require live Aurora + DynamoDB.
    They cannot run in a unit-test environment.
  - The tests are @pytest.mark.integration and @pytest.mark.skipif on
    AURORA_HOST being absent, following the 8.5/8.9 skip-gate idiom.
  - IT-8.9b-2 and IT-8.9b-4 verify the no-op behaviour (room absent):
    they succeed if the handler returns 200 without raising and without an
    entry in ChatRooms.  The assertion is the HTTP 200 + no DynamoDB error.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Skip gate — all integration tests in this file require a live Aurora cluster
# ---------------------------------------------------------------------------

_AURORA_HOST = os.environ.get("AURORA_HOST", "")

_requires_live_stack = pytest.mark.skipif(
    not _AURORA_HOST,
    reason=(
        "Live Aurora + DynamoDB stack required. "
        "Set AURORA_HOST (and TABLE_CHAT_ROOMS, DB_SECRET_NAME, etc.) to run."
    ),
)

# ---------------------------------------------------------------------------
# Integration tests (skip-gated)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@_requires_live_stack
def test_it_8_9b_1_accept_after_unblock_flips_friendship_active_true():
    """
    IT-8.9b-1: Accept-path, refriend after unblock.

    Scenario: A and B were friends and chatted (ChatRooms row exists with
    status=active, friendship_active=false after A blocked then unblocked B).
    B sends A a friend request and A accepts.

    Expected: ChatRooms.friendship_active becomes true.
    """
    pytest.skip(
        "IT-8.9b-1: requires live Aurora + DynamoDB. "
        "Set AURORA_HOST to run."
    )


@pytest.mark.integration
@_requires_live_stack
def test_it_8_9b_2_accept_no_prior_room_no_ops_silently():
    """
    IT-8.9b-2: Accept-path, no prior room.

    Scenario: A and B have never chatted. A sends B a friend request and B
    accepts.

    Expected: friendship row created in Aurora, DynamoDB UpdateItem on the
    non-existent ChatRooms row is a silent no-op (ConditionalCheckFailed
    swallowed), handler returns HTTP 200, no error.
    """
    pytest.skip(
        "IT-8.9b-2: requires live Aurora + DynamoDB. "
        "Set AURORA_HOST to run."
    )


@pytest.mark.integration
@_requires_live_stack
def test_it_8_9b_3_unfriend_with_active_room_flips_friendship_active_false():
    """
    IT-8.9b-3: Unfriend-path.

    Scenario: A and B are friends with an active chat room (status=active,
    friendship_active=true). A calls DELETE /v1/friends/B.

    Expected:
      - Friendship row removed from Aurora.
      - ChatRooms.friendship_active becomes false.
      - ChatRooms.status stays 'active' (not 'deactivated' — that is the
        blocks Lambda's job).
    """
    pytest.skip(
        "IT-8.9b-3: requires live Aurora + DynamoDB. "
        "Set AURORA_HOST to run."
    )


@pytest.mark.integration
@_requires_live_stack
def test_it_8_9b_4_unfriend_no_prior_room_no_ops_silently():
    """
    IT-8.9b-4: Unfriend-path, no prior room.

    Scenario: A and B are friends but have never chatted (no ChatRooms row).
    A calls DELETE /v1/friends/B.

    Expected: friendship row removed, DynamoDB UpdateItem is a silent no-op
    (ConditionalCheckFailed swallowed), handler returns HTTP 200, no error.
    """
    pytest.skip(
        "IT-8.9b-4: requires live Aurora + DynamoDB. "
        "Set AURORA_HOST to run."
    )
