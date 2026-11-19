phase: friend-request-correctness (hotfix, not a numbered phase)
title: Friend-request correctness — GET direction filter, reverse/already-friends checks, Notifications-row writes
created: 2026-11-19
owner: backenddeveloper
status: draft
depends_on: [phase 6 (friends Lambda), phase 8 (notifications_publisher)]

context_summary: |
  A multi-device test of the production frontend uncovered four backend
  correctness bugs in the friend-request flow. All are **pre-existing** (not
  regressions from any recent change); they went unnoticed because
  single-device development exercises only half of each flow.

  **Repro — the trigger case:**
    1. User A sends a friend request to User B.        [expect: B sees modal]
    2. B does NOT see any in-app modal.
    3. Out of curiosity, B sends a friend request to A. [expect: 409 / auto-accept]
    4. The request succeeds; a second `friend_requests` row is inserted
       `(B → A, pending)` alongside the existing `(A → B, pending)`.
    5. B opens the Requests tab; sees TWO rows — A's request (legit) AND B's
       OWN outgoing request to A, displayed as if it were incoming.

  **Four root causes, each needs a story below:**

  RC-1. `friends/handler.py` never writes a row to the DynamoDB `Notifications`
    table on friend-request create or accept. The `notifications_publisher`
    Lambda (phase 8.9c) consumes the Notifications stream and fans out to
    AppSync, but it has zero producers for friend-request events. Both
    frontend modals (`IncomingRequestModal`, `RequestAcceptedModal`) are
    wired to `onFriendRequestUpdated` / `onNotificationForMe` subscriptions
    and will never fire until this is fixed. See story FR-5 / FR-6.

  RC-2. `_handle_get_friend_requests` ignores the `direction` query
    parameter — it always returns both incoming AND outgoing pending
    requests. The frontend calls `GET /v1/friend-requests?direction=incoming`
    expecting only requests sent TO the caller; it currently receives both.
    The user-visible symptom is B seeing her own outgoing request to A
    rendered as an "incoming" row. See story FR-1.

  RC-3. The POST handler has no reverse-pending check. The UNIQUE constraint
    on `friend_requests` is `(from_user_id, to_user_id, status)` — which
    prevents duplicate same-direction inserts (A→B twice) but NOT
    reverse-direction inserts (A→B exists → B→A still inserts). Allowing
    the reverse insert would mean accepting one direction creates the
    friendship while the other direction's row remains pending forever,
    and a subsequent accept would attempt a duplicate friendships INSERT
    (safely handled by `ON CONFLICT DO NOTHING`, but leaves orphan rows).
    See story FR-2.

  RC-4. The POST handler has no already-friends check. If A and B are already
    in the `friendships` table and A sends a friend request to B, the
    insert succeeds and B sees a "new" request in their incoming list. See
    story FR-3.

  The frontend will ship a parallel branch (`fix/pre-send-and-incoming-polling`)
  with two client-side mitigations:
  (a) client-side filter on the incoming list to drop rows where
      `from_user_id === currentUserId` (safety net for RC-2);
  (b) poll-based diff of the incoming query result to fire the
      `IncomingRequestModal` locally when a new `request_id` appears,
      without waiting for the AppSync push (interim workaround for RC-1).
  Both frontend mitigations are additive and safe to leave in place after
  this backend hotfix lands — they become no-ops once the backend contract
  is correct.

stories:

  - id: FR-1
    title: Honour `direction=incoming|outgoing` query param on GET /v1/friend-requests
    agent: backenddeveloper
    depends_on: []
    acceptance_criteria:
      - "`_handle_get_friend_requests` parses `event['queryStringParameters']['direction']` and accepts exactly one of `'incoming'`, `'outgoing'`, or missing (=both, preserved for backward compat with any caller that doesn't pass direction)."
      - When `direction == 'incoming'`: WHERE clause narrows to `fr.to_user_id = %s::uuid` only. The caller sees only requests sent TO them.
      - When `direction == 'outgoing'`: WHERE clause narrows to `fr.from_user_id = %s::uuid` only. The caller sees only requests they sent.
      - When `direction` is missing: current behaviour preserved (both directions returned). This keeps the endpoint backward-compatible for internal callers that don't pass direction.
      - When `direction` is any other value (e.g. `'both'`, `'foo'`): return 400 `{"error": "invalid_direction"}` so callers don't silently get the wrong data.
      - `block_filter` is still applied to BOTH `fr.from_user_id` AND `fr.to_user_id` regardless of direction (so a blocked user's requests are filtered from both incoming and outgoing views).
      - The `status = 'pending'` filter is preserved unchanged.
      - Add integration tests covering: incoming returns incoming-only; outgoing returns outgoing-only; missing returns both; invalid returns 400. Use the `completed_profile_user` fixture pair.
      - Update the Lambda's docstring + `## Routes` summary to document the three directions.
    notes: |
      The existing frontend already calls with `direction=incoming` and
      `direction=outgoing`. This story makes the backend honour the
      contract it was already expected to.

  - id: FR-2
    title: Reject reverse-pending friend requests on POST /v1/friend-requests
    agent: backenddeveloper
    depends_on: []
    acceptance_criteria:
      - "`_handle_post_friend_requests` adds a reverse-pending check INSIDE the same transaction as the `is_blocked` check (same transactional guarantee reasoning — a concurrent request creation must not race through)."
      - "The check: `SELECT 1 FROM friend_requests WHERE from_user_id = %s::uuid AND to_user_id = %s::uuid AND status = 'pending' FOR UPDATE` using `(target_user_id, requester_id)` (reverse of what the new row would insert)."
      - If a row is returned, respond 409 `{"error": "reverse_pending", "request_id": "<the reverse row's id>"}` and DO NOT insert the new row. The frontend uses `request_id` to offer the user "they also want to be friends — accept their request" UX.
      - The existing `UniqueViolation → 409 {"error": "already_pending"}` path is preserved for same-direction duplicates.
      - "Audit decision: this story does NOT auto-accept when reverse-pending is detected. Auto-accept would be a product-level decision affecting UX (the user hasn't seen the other's request yet) — leave it to the frontend to offer the accept option after the 409."
      - Integration tests covering: reverse-pending triggers 409 with correct error code + request_id; same-direction duplicate still triggers `already_pending`; no reverse pending → request succeeds (regression).
      - Update docstring `## Transaction semantics` to document the new reverse-pending branch.

  - id: FR-3
    title: Reject new friend requests when caller and target are already friends
    agent: backenddeveloper
    depends_on: []
    acceptance_criteria:
      - "`_handle_post_friend_requests` adds an already-friends check INSIDE the same transaction as the block + reverse-pending checks (all three run before any INSERT)."
      - "The check uses canonical pair ordering: `SELECT 1 FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid` with `(min(caller, target), max(caller, target))`."
      - If a row is returned, respond 409 `{"error": "already_friends"}` and DO NOT insert the new friend_requests row.
      - The three 409 branches are ordered: (1) blocked, (2) already_friends, (3) reverse_pending, (4) already_pending (via UniqueViolation). Order matters for UX — blocked takes precedence so the blocker's state is not leaked by a different error code.
      - Integration tests covering: already-friends returns 409 with `error: 'already_friends'`; not-yet-friends succeeds (regression).
      - Update docstring to document the new 409 code.

  - id: FR-4
    title: Reject new friend requests when caller has already sent a pending request to target
    agent: backenddeveloper
    depends_on: []
    acceptance_criteria:
      - "This is already enforced by the DB-level UNIQUE constraint `(from_user_id, to_user_id, status)` from migration 0005. The current handler translates `psycopg2.errors.UniqueViolation` into 409 `{\"error\": \"already_pending\"}` — verify that path is covered by an integration test."
      - "No code change required UNLESS the integration test reveals the UniqueViolation path is not being hit (e.g. race between two concurrent POSTs). If racy, wrap the pre-check `SELECT 1 FROM friend_requests WHERE from_user_id = %s AND to_user_id = %s AND status = 'pending'` in the same transaction as FR-2 / FR-3 and return the same 409 explicitly."
      - Integration test covering: sending the same request twice → first 201, second 409 `{"error": "already_pending"}`.
    notes: |
      Likely a verification-only story. Include anyway because the
      frontend will show a user-facing message for this case and the test
      coverage proves the contract.

  - id: FR-5
    title: Write Notifications row on POST /v1/friend-requests to trigger `onNotificationForMe` for recipient
    agent: backenddeveloper
    depends_on: [FR-2, FR-3, FR-4]
    acceptance_criteria:
      - After the Aurora transaction in `_handle_post_friend_requests` commits successfully, PutItem into DynamoDB `Notifications` table with the schema the `notifications_publisher` Lambda (phase 8.9c) expects.
      - "Notifications row fields: `user_id = <recipient_id>` (PK — the person receiving the request; drives subscription filtering), `created_at_notification_id = <ISO8601>#<ulid>` (SK), `notification_id = <ulid>`, `type = 'friend_request_received'`, `payload = JSON_STRING({request_id, from_user_id})`, `sender_user_id = <requester_id>`, `sender_name = <requester_first_name>` (looked up from users table in the same request), `read = false`, `delivered = false`, `created_at = <ISO8601>`."
      - The PutItem is OUTSIDE the Aurora transaction — if it fails, the request itself still succeeds; the failure is logged + the response carries an optional `notification_pending: true` field (same pattern as `_set_friendship_active`).
      - The frontend subscription `onNotificationForMe` will receive this via `notifications_publisher` → `publishNotification` fan-out (already wired in phase 8.9c, no changes needed there — this story just supplies the producer).
      - Integration test — end-to-end via live AppSync subscription — covering: POST request → Notifications row written → subscription event observed on recipient's AppSync websocket within 10s. Use the `appsync_subscription` test helper pattern from phase 8.
      - "IAM: grant `notifications_table_writer` role (or extend the friends Lambda's role) with `dynamodb:PutItem` on the Notifications table ARN."
      - "Terraform: add the IAM grant + Notifications table env var to the friends lambda module."
      - Update docstring + `## Side effects` list.
    notes: |
      The notifications_publisher consumes the Notifications stream.
      `type='friend_request_received'` routes to `publishNotification` →
      `onNotificationForMe` subscription per
      notifications_publisher/handler.py lines 158-188.

      **Frontend contract change required** (coordinate with knotify-frontend):
      the subscription listener currently only watches
      `onFriendRequestUpdated`. It needs to ALSO subscribe to
      `onNotificationForMe` and branch on `notification.type` —
      `friend_request_received` → open IncomingRequestModal. That change
      ships in the parallel frontend branch
      `fix/pre-send-and-incoming-polling`.

  - id: FR-6
    title: Write Notifications row on POST /v1/friend-requests/{id}/accept to trigger `onFriendRequestUpdated` for sender
    agent: backenddeveloper
    depends_on: [FR-5]
    acceptance_criteria:
      - After the Aurora transaction in `_handle_accept_friend_request` commits (friend_requests UPDATE + friendships INSERT), PutItem into DynamoDB `Notifications` table addressed to the ORIGINAL SENDER (not the acceptor).
      - "Notifications row fields: `user_id = <original_sender_id>` (PK — the person who sent the request; now being notified of acceptance), `created_at_notification_id = <ISO8601>#<ulid>` (SK), `notification_id = <ulid>`, `type = 'friend_request_accepted'`, `payload = JSON_STRING({request_id, from_user_id: <acceptor_id>})`, `sender_user_id = <acceptor_id>`, `sender_name = <acceptor_first_name>`, `read = false`, `delivered = false`, `created_at = <ISO8601>`."
      - The PutItem happens AFTER the DynamoDB `_set_friendship_active` call and BEFORE the response is built. Failure mode mirrors FR-5: log + response gets `notification_pending: true`.
      - `notifications_publisher` routes `type='friend_request_accepted'` to `_publishFriendRequestUpdated` → `onFriendRequestUpdated` subscription (already wired; no changes).
      - Integration test — end-to-end via live AppSync subscription — covering: accept → Notifications row written → subscription event observed on sender's AppSync websocket.
      - Update docstring.
    notes: |
      This story completes the modal flow for BOTH the incoming
      friend-request modal (FR-5) AND the request-accepted modal (this
      story). The frontend's existing `onFriendRequestUpdated`
      subscription already handles the `friend_request_accepted` branch;
      it just needs this producer to start writing.

  - id: FR-7
    title: End-to-end regression suite covering FR-1 … FR-6
    agent: backenddeveloper
    depends_on: [FR-1, FR-2, FR-3, FR-4, FR-5, FR-6]
    acceptance_criteria:
      - Add a new integration test file `infrastructure/src/tests/integration/test_friend_requests_e2e.py` that walks the complete flow using the `completed_profile_user(sex)` fixture pair.
      - "Test sequence (one test function, sequential assertions):
          1. A sends to B → 201, Notifications row written for B, subscription event observed
          2. B's GET `?direction=incoming` → sees A's request (and ONLY A's request)
          3. B's GET `?direction=outgoing` → empty
          4. B attempts to send to A → 409 `reverse_pending` with the original request_id
          5. A attempts to send to B again → 409 `already_pending`
          6. B accepts A's request → 200, friendships row exists, Notifications row written for A, subscription event observed for A
          7. A attempts to send another request to B → 409 `already_friends`
          8. A unfriends B → 200
          9. A sends to B → 201 (regression: unfriending restores ability to re-send)."
      - "Teardown must be idempotent: DELETE FROM friendships + friend_requests + Notifications for the two test user_ids, regardless of which assertion failed."
      - "Test is skip-gated on `KNOTIFY_RUN_INTEGRATION=1` (match existing convention in `test_domains_e2e.py`)."
    notes: |
      This story proves the full contract works end-to-end before the
      frontend consumes it. Without this story the frontend team has no
      way to verify their subscription-listener rewire against a live
      backend.

carryovers_to_phase_11_hardening: |
  - None from this hotfix. Reverse-pending auto-accept (if ever desired)
    is a product decision, not a hardening concern.

carryovers_to_phase_8_chat: |
  - None.

frontend_coordination_notes: |
  The parallel frontend branch `fix/pre-send-and-incoming-polling` ships:
    - Client-side filter: `useIncomingRequestsQuery` drops rows where
      `from_user_id === currentUserId`. Safe to leave in place after FR-1
      lands — becomes a no-op.
    - Poll-based modal detection: `FriendRequestSubscriptionListener`
      diffs successive `['friend-requests','incoming']` query data; new
      `request_id` → opens `IncomingRequestModal`. Workaround for RC-1.
      After FR-5 lands, the AppSync subscription will also fire; the
      listener should keep the poll-based path as a belt-and-braces
      fallback, dedupe by `request_id` so the modal doesn't open twice.
    - Pre-send guards on every `useSendFriendRequestMutation` callsite:
      `useFriendsQuery.data` for "already connected", 
      `useOutgoingRequestsQuery.data` for "already sent". These are UX
      pre-flight checks; the backend 409s from FR-2/FR-3/FR-4 are the
      source of truth.
    - AFTER FR-5 lands: frontend subscribes to `onNotificationForMe` and
      routes `type === 'friend_request_received'` to
      `IncomingRequestModal`. At that point the poll-based detection can
      be kept or removed — recommend keeping as a fallback for
      subscription-drop scenarios.

acceptance_summary: |
  When this hotfix is complete, a QA pass against the production
  frontend should confirm:
    - B sees the IncomingRequestModal when A sends her a request.
    - A sees the RequestAcceptedModal when B accepts.
    - B's Requests tab shows only incoming requests (no self-rows).
    - Any attempt to send a reverse / duplicate / already-friends request
      returns a 409 with a specific error code the frontend can map to a
      sensible user-facing message.
