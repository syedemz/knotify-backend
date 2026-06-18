phase: 8
title: Chat (AppSync + DynamoDB Streams + push fan-out)
last_updated: 2026-06-17 # story 8.6 done

context_summary: |
  Delivers the full chat capability in a single phase per the owner's resolved Option A: the AppSync GraphQL API with a hand-written schema (no Amplify auto-generation, no auto-CRUD subscriptions), Lambda resolvers that enforce membership and block checks against Aurora before establishing subscriptions, the deterministic-room-id creation flow from §5.4.1, DynamoDB Streams from ChatMessages and Notifications wired to a PushFanout Lambda that targets Expo Push (per the §13 #7 resolution in v1.6), the POST /v1/push-tokens REST endpoint for token registration, and the stale-token cleanup scheduled Lambda. This phase intentionally ships data plane and API plane together because the GraphQL schema and the DynamoDB key design are tightly coupled. After this phase only account deletion, observability consolidation, hardening, and S3 photos remain.

## Carryovers from phase 6
- Chat-room-without-backing-friendship is read-only — phase-6 story 6.4 deletes the friendship row on POST /v1/blocks and reactivates the chat room on DELETE /v1/blocks, but does NOT recreate the friendship on unblock. Phase 8's chat-write path must treat an `active` ChatRooms row whose canonical pair has no `friendships` entry as read-only. Source: phase-6 third-pass Md1.
  - Implementation: cache a `friendship_active` boolean on the ChatRooms row, written by the friends and blocks Lambdas; sendMessage rejects when false. Story 8.9 covers block-side maintenance; story 8.9b covers friend/accept-side maintenance.

## Carryovers from phase 7 / hotfix lessons
- Hotfix #86 trap: every `aws_lambda_permission` for API Gateway routes MUST use `module.api_gateway.api_execution_arn`, NEVER `default_stage_arn`. Stories 8.11 reinforces this explicitly.
- Hotfix #106 trap: Lambdas placed in private subnets cannot reach the open internet without NAT or pre-wired VPC endpoints. Story 8.10 (PushFanout) runs OUTSIDE the VPC for exactly this reason.
- Hotfix #87 trap: any Aurora SQL doing block exclusion MUST use `knotify_db.block_filter("alias.user_id")` not hand-rolled NOT EXISTS subqueries. Story 8.0 surfaces this for the chat resolver.
- Hotfix #109 / phase-7 JWT propagation: `custom:user_sex` and `custom:profile_complete` claims are already emitted by `cognito_pre_token_generation`. Chat resolvers read them from `event.identity.claims` — no new pre-token-gen wiring needed.

## Cross-cutting decisions
- **Chat-resolver Lambda scaffold lives in its own story (8.0)** mirroring phase-7's 7.0 split. Subsequent resolver stories (8.3 / 8.4 / 8.5 / 8.7 / 8.8) slot Python into the existing dispatcher.
- **Backend-published AppSync events** use DynamoDB-Streams-driven publisher Lambdas rather than calling AppSync from domain Lambdas directly. Two publishers: `room_state_publisher` (story 8.9a, on ChatRooms — `_publishRoomDeactivated` / `_publishRoomReactivated`) and `notifications_publisher` (story 8.9c, on Notifications — `publishNotification` / `_publishFriendRequestUpdated`). Keeps the blocks and friends Lambdas DDB-and-Aurora-only.
- **PushFanout runs OUTSIDE the VPC.** It only touches DynamoDB (no VPC required) and Expo (open internet). Inside-VPC would repeat hotfix #106.
- **Profile-complete gate**: applied to AppSync chat resolvers via a parallel `@require_profile_complete_appsync` decorator (story 8.0); NOT applied to `POST /v1/push-tokens` (story 8.11) because tokens register at app launch, before onboarding.
- **`@aws_subscribe` field filters**: every subscription declared in story 8.2 carries an explicit `field` filter matching the publishing mutation's argument (e.g. `roomId`).
- **Makefile.** Every new Lambda (chat_resolver, room_state_publisher, notifications_publisher, push_fanout, push_tokens, stale_token_cleanup) is added to `Makefile`'s `package-all` target in its own story.

stories:
  - id: 8.0
    title: Chat resolver Lambda scaffold + IAM + Terraform module
    agent: backenddeveloper
    tracking_issue: 110
    done: true
    depends_on: []
    acceptance_criteria:
      - infrastructure/src/functions/chat_resolver/ directory with handler.py (empty dispatcher returning a structured Unimplemented error for every (typeName, fieldName) until later stories slot logic in), __init__.py, requirements.txt, and tests/test_chat_resolver.py (smoke: dispatcher returns Unimplemented for unknown fields, unit tests pass)
      - chat_resolver IAM role in infrastructure/modules/iam_roles/ with VPC execution policy, scoped GetSecretValue on the Aurora app_user_credential, and dynamodb actions (GetItem, PutItem, UpdateItem, Query, BatchGetItem, TransactWriteItems) on the five chat tables (ChatRooms, ChatRoomMembership, ChatMessages, MessageReads, Notifications) — table ARNs sourced from module.dynamodb outputs
      - Extend `infrastructure/modules/dynamodb/outputs.tf` with table-ARN outputs for the six chat-domain tables that downstream phase-8 stories reference: `chat_rooms_arn`, `chat_room_membership_arn`, `chat_messages_arn`, `message_reads_arn`, `notifications_arn`, `push_notification_tokens_arn` (each `value = aws_dynamodb_table.<name>.arn`). The module currently only exports `*_table_name` plus `chat_messages_stream_arn` and `notifications_stream_arn`; without the table ARNs, the IAM policies in 8.0, 8.9, 8.9b, 8.10, 8.11, and 8.12 fail `terraform validate` on a missing exported attribute. The `chat_rooms_stream_arn` output is added later by story 8.9a alongside enabling the stream itself — out of scope here.
      - Terraform module infrastructure/modules/chat_resolver/ provisions the Lambda (ARM64, in-VPC, knotify_db + knotify_obs layers, 30-second timeout, environment vars AURORA_HOST/PORT/DBNAME + DB_SECRET_NAME populated from the existing app_user_credential pattern)
      - Module exported `lambda_arn` is consumed by the AppSync module in story 8.1 to register the Lambda data source
      - Dev and prod root modules (infrastructure/dev/main.tf, infrastructure/prod/main.tf) wire `module "chat_resolver"`
      - knotify_obs layer gets a new `@require_profile_complete_appsync` decorator that reads `event["identity"]["claims"]["custom:profile_complete"]` and returns Unauthorized when not "true" — mirrors the REST decorator from phase-7 7.0b
      - knotify_db.block_filter() is the only acceptable mechanism for any block-exclusion SQL added in later chat stories — hand-rolled NOT EXISTS forbidden (hotfix #87 lesson)
      - Makefile package-all extended with chat_resolver.zip target
      - terraform validate clean in dev and prod
    notes: "Mirrors phase-7 7.0. Subsequent resolver stories (8.3, 8.4, 8.5, 8.7, 8.8) extend handler.py with concrete (typeName, fieldName) implementations."

  - id: 8.1
    title: AppSync API Terraform module
    agent: backenddeveloper
    tracking_issue: 111
    done: true
    depends_on: [8.0]
    acceptance_criteria:
      - infrastructure/modules/appsync/main.tf creates aws_appsync_graphql_api with authentication_type AMAZON_COGNITO_USER_POOLS (primary) and additional_authentication_provider AWS_IAM (secondary, consumed by the backend publisher Lambdas — 8.9a room_state_publisher and 8.9c notifications_publisher; publish mutations declared in 8.2 are annotated `@aws_iam` so user-JWT clients cannot invoke them)
      - user_pool_config block declared with user_pool_id = module.cognito.user_pool_id, aws_region = data.aws_region.current.name, default_action = "DENY"
      - new appsync_logs_role in infrastructure/modules/iam_roles/ with trust on appsync.amazonaws.com and managed policy AWSAppSyncPushToCloudWatchLogs; passed into log_config.cloudwatch_logs_role_arn
      - log_config writes to CloudWatch at FIELD level with 7-day retention
      - aws_appsync_datasource resources declared for the five DynamoDB tables (ChatRooms, ChatRoomMembership, ChatMessages, MessageReads, Notifications) — table ARNs sourced from module.dynamodb outputs
      - aws_appsync_datasource of type AWS_LAMBDA named `chat_resolver_ds` registered against module.chat_resolver.lambda_arn (this is the consumer of story 8.0's Lambda)
      - Module outputs api_id, graphql_url, realtime_url, and the chat_resolver_ds name (so later stories can attach resolvers to it)
      - aws_iam_role for AppSync to invoke chat_resolver Lambda is declared and granted lambda:InvokeFunction on module.chat_resolver.lambda_arn
      - terraform validate clean in dev and prod
    notes: ""

  - id: 8.2
    title: Hand-written GraphQL schema
    agent: backenddeveloper
    tracking_issue: 112
    done: true
    depends_on: [8.1]
    acceptance_criteria:
      - File infrastructure/modules/appsync/schema.graphql defines Message, ChatRoom, ChatRoomMembership, MessageRead, Notification, TypingEvent types with the field shapes from §5.4 of architecture.md
      - The Subscription type declares ONLY these scoped subscriptions, each `@aws_subscribe`-bound to the named publishing mutation with an explicit `field` filter matching the mutation's primary argument
          - onMessageInRoom(roomId: ID!) → mutation sendMessage, field filter roomId
          - onNotificationForMe → mutation publishNotification (backend-only, @aws_iam), filter by identity.sub
          - onTypingInRoom(roomId: ID!) → mutation setTyping, field filter roomId
          - onRoomDeactivated(roomId: ID!) → mutation _publishRoomDeactivated (backend-only, @aws_iam), field filter roomId
          - onRoomReactivated(roomId: ID!) → mutation _publishRoomReactivated (backend-only, @aws_iam), field filter roomId
          - onReadReceipt(roomId: ID!) → mutation markAsRead, field filter roomId
          - onFriendRequestUpdated → mutation _publishFriendRequestUpdated (backend-only, @aws_iam), filter by identity.sub
      - All backend-only publish mutations carry the `@aws_iam` directive — JWT clients cannot invoke them
      - The schema contains no auto-generated onCreateMessage/onUpdateMessage/onDeleteMessage subscriptions
      - The sendMessage mutation signature has no senderId argument; sender is derived server-side
      - aws_appsync_graphql_api.schema points at this file; terraform apply succeeds and the introspection query returns the declared schema
    notes: "The publish mutations (`_publishRoomDeactivated`, `_publishRoomReactivated`, `_publishFriendRequestUpdated`, `publishNotification`) exist solely so AppSync has a known mutation to broadcast through. They are invoked from backend Lambdas via SigV4 (IAM mode) — see story 8.9a for the room-state publisher."

  - id: 8.3
    title: createOrGetRoom resolver (idempotent room creation)
    agent: backenddeveloper
    tracking_issue: 113
    done: true
    depends_on: [8.0, 8.1, 8.2]
    acceptance_criteria:
      - chat_resolver dispatcher routes (Mutation, createOrGetRoom) to a handler implementing the §5.4.1 flow: verifies caller != other user; queries Aurora for friendship (both directions) via knotify_db; queries Aurora for blocks (both directions) via knotify_db.is_blocked / block_filter helper; computes room_id = sha256(canonical_pair(caller, other)) via knotify_obs.chat_room_id
      - Attempts ChatRooms PutItem with attribute_not_exists(room_id) — on success, ChatRooms row carries `status='active'` and `friendship_active=true` (the friendship check just succeeded); the same TransactWriteItems batch inserts both ChatRoomMembership rows
      - On ConditionalCheckFailedException (room exists) proceeds without writing; returns the existing room
      - @require_profile_complete_appsync decorator applied (caller must have custom:profile_complete = "true")
      - Integration test: A and B are friends, A calls createOrGetRoom(B) twice → same room_id, exactly one ChatRooms row, exactly two ChatRoomMembership rows
      - Integration test: A and C are not friends → returns Unauthorized with reason NOT_FRIENDS
      - Integration test: A has blocked B → returns Unauthorized with reason BLOCKED
      - Integration test: caller without custom:profile_complete claim → returns Unauthorized with reason PROFILE_INCOMPLETE
    notes: "Completed 2026-06-17. _handle_create_or_get_room in handler.py; 16 unit tests pass; 4 integration tests authored + skip-gated (IT-8.3-4 passes without live env)."

  - id: 8.4
    title: sendMessage resolver (with idempotency + friendship-active gate)
    agent: backenddeveloper
    tracking_issue: 114
    done: true
    depends_on: [8.3]
    acceptance_criteria:
      - chat_resolver dispatcher routes (Mutation, sendMessage) to a handler that performs: GetItem ChatRoomMembership(sender, roomId), reject Unauthorized if missing; GetItem ChatRooms(roomId), reject RoomDeactivated if status != 'active', reject RoomReadOnly if friendship_active != true; insert ChatMessages with server-set sender_id (from identity.sub) and delivered_at=NOW() and SK = `<iso-timestamp>#<ulid>`
      - The ChatMessages PutItem and the ChatRooms UpdateItem (last_message_id, last_message_at, last_message_preview, last_message_sender_id) are issued in a single TransactWriteItems call
      - TransactWriteItems carries a ClientRequestToken computed server-side as `sha256(sender_id + "|" + room_id + "|" + content + "|" + floor(epoch_seconds))` — a deterministic hash of (sender, room, content, second-precision wall-clock). The resolver computes it before issuing the TransactWriteItems call; retries within the same wall-clock second produce the identical token, so DynamoDB's 10-minute idempotency window deduplicates. No client-supplied argument is added to the sendMessage mutation signature — the schema in 8.2 stays clean.
      - sender_id is NEVER read from the GraphQL arguments
      - No Notifications row is written for chat messages (§5.4.2 explicit rule)
      - @require_profile_complete_appsync applied
      - Integration test: A sends a message in a room A and B share → ChatMessages row appears with sender_id=A, content_type=text, delivered_at within 1 second of the call
      - Integration test: a user not in the room attempts sendMessage with the room_id → resolver returns Unauthorized
      - Integration test: room.status == 'deactivated' → sendMessage returns RoomDeactivated
      - Integration test: room.status == 'active' but room.friendship_active == false → sendMessage returns RoomReadOnly
      - Unit test (in tests/test_chat_resolver.py, NOT against a deployed Lambda): the token-derivation function is pure — same (sender, room, content, second) inputs produce the same hash; differing inputs (including content differing by one byte, or second offset by ±1) produce different hashes. This is the deterministic-token correctness proof; the deployed Lambda cannot have its wall clock pinned, so an integration-test replay would be flaky and is intentionally omitted. The TransactWriteItems 10-minute idempotency window is an AWS-side guarantee — we cover it by proving the token is pure.
    notes: "Carries the post-block-unblock-no-refriend case (friendship_active=false on a status=active room). Story 8.9 maintains this flag on block; story 8.9b maintains it on friend-request accept."

  - id: 8.5
    title: Query resolvers listMyRooms and messagesByChatRoom
    agent: backenddeveloper
    tracking_issue: 115
    done: true
    depends_on: [8.3]
    acceptance_criteria:
      - listMyRooms: chat_resolver Query handler issues DynamoDB Query on ChatRoomMembership with PK=identity.sub, collects the room_id values; calls BatchGetItem(ChatRooms, Keys=[{room_id: r1}, {room_id: r2}, ...]) to fetch each room object; merges; returns the list ordered by ChatRooms.last_message_at descending
      - BatchGetItem 100-item cap: for v1 nobody will exceed; document the cap in handler.py with a TODO referencing phase 11 hardening if it ever becomes a concern
      - messagesByChatRoom(roomId, createdAtGt?, limit?): GetItem(ChatRoomMembership, (identity.sub, roomId)) membership check, reject Unauthorized if missing; Query on ChatMessages PK=roomId, ScanIndexForward=False (descending by SK), SK condition > createdAtGt when supplied; paginated via nextToken (DynamoDB LastEvaluatedKey, base64-encoded)
      - @require_profile_complete_appsync applied to both resolvers
      - Integration test: messagesByChatRoom returns the latest 20 messages by default; passing createdAtGt fetches only newer messages
      - Integration test: listMyRooms for a user with two rooms returns both, in last_message_at descending order
      - Integration test: messagesByChatRoom for a user not in the room returns Unauthorized
    notes: ""

  - id: 8.6
    title: Scoped subscriptions with pipeline membership check
    agent: backenddeveloper
    tracking_issue: 116
    done: true
    depends_on: [8.3, 8.4]
    acceptance_criteria:
      - Each subscription (onMessageInRoom, onTypingInRoom, onRoomDeactivated, onRoomReactivated, onReadReceipt) is backed by a pipeline resolver whose first function checks ChatRoomMembership(identity.sub, roomId); on miss the resolver returns Unauthorized and the WebSocket subscription fails to establish
      - onNotificationForMe is identity-scoped: pipeline first function filters on user_id == identity.sub
      - @aws_subscribe field-filter declarations confirmed working: subscriber sees only events where the filter field on the mutation result matches the subscription argument
      - Integration test: a user not in room R attempts subscription onMessageInRoom(R) → connection rejected before any message can be received
      - Integration test: A and B in room R, A sends a message → B's onMessageInRoom subscription receives the Message within 2 seconds
      - Integration test: A and B in room R, A sends a message → a third user C subscribed to onMessageInRoom(R2) for a different room does NOT receive the event (field filter enforced)
    notes: "Completed 2026-06-17. APPSYNC_JS runtime on ChatRoomMembership DDB datasource (check_room_membership) + NONE datasource (check_identity_match). 7 PIPELINE resolvers, 7 new TF module tests (15 total), 3 integration tests skip-gated on APPSYNC_GRAPHQL_URL. terraform validate clean dev+prod."

  - id: 8.7
    title: markAsRead mutation and read-receipt updates
    agent: backenddeveloper
    tracking_issue: 117
    done: false
    depends_on: [8.4]
    acceptance_criteria:
      - markAsRead(roomId, lastMessageId) resolver: TransactWriteItems updates MessageReads(PK=roomId, SK=identity.sub) with last_read_message_id + last_read_at AND ChatRoomMembership(identity.sub, roomId) with the same cached values
      - The mutation result is the type subscribed by onReadReceipt(roomId) — schema in 8.2 confirms onReadReceipt is `@aws_subscribe(mutations: ["markAsRead"])` with field filter roomId
      - Membership check (GetItem ChatRoomMembership) before any write; reject Unauthorized on miss
      - @require_profile_complete_appsync applied
      - Integration test: A sends message m1, B calls markAsRead(R, m1) → MessageReads has B's row with last_read_message_id=m1; A's onReadReceipt subscription receives the event within 2 seconds
      - Integration test: a user not in the room attempts markAsRead → Unauthorized
    notes: ""

  - id: 8.8
    title: setTyping mutation with no storage
    agent: backenddeveloper
    tracking_issue: 118
    done: false
    depends_on: [8.6]
    acceptance_criteria:
      - setTyping(roomId, isTyping) uses an AppSync None data source; the resolver validates membership (GetItem ChatRoomMembership) and returns the payload to be fanned out via onTypingInRoom
      - No DynamoDB write occurs (verified by examining a CloudTrail trace of the mutation call)
      - Integration test: A calls setTyping(R, true) → B's onTypingInRoom subscription receives {userId: A, isTyping: true}
    notes: ""

  - id: 8.9
    title: Extend the knotify-blocks Lambda for chat-room deactivation / reactivation
    agent: backenddeveloper
    tracking_issue: 119
    done: false
    depends_on: [8.3, 8.4]
    acceptance_criteria:
      - 1. CODE EDIT (infrastructure/src/functions/blocks/handler.py)
          - On POST /v1/blocks (block path), after the existing Aurora work, perform a DynamoDB UpdateItem on ChatRooms(room_id = knotify_obs.chat_room_id(blocker, blocked)) setting status='deactivated', deactivated_reason='blocked', deactivated_by=blocker, deactivated_at=NOW(), friendship_active=false. The UpdateItem is conditional on attribute_exists(room_id) — if no chat room ever existed between the pair, the update silently no-ops.
          - On DELETE /v1/blocks (unblock path), perform the §5.4.1 conditional UpdateItem: SET status='active', reactivated_at=NOW(), REMOVE deactivated_reason, deactivated_at, deactivated_by — but ONLY if `deactivated_reason='blocked' AND deactivated_by=current_user`. friendship_active stays false (the unblock does not restore the friendship; only a subsequent friend-request accept does, via story 8.9b).
      - 2. IAM POLICY UPDATE (infrastructure/modules/iam_roles/)
          - aurora_writer (or the role attached to the blocks Lambda) gains scoped dynamodb:UpdateItem on the ChatRooms table ARN. Table ARN sourced from module.dynamodb output, not hardcoded.
      - 3. REDEPLOY VIA CI (NOT manual `terraform apply`)
          - Makefile package-all rebuilds blocks.zip. Commit is pushed via the standard feature-branch → PR → development merge flow; the CI/CD pipeline applies the terraform change. The story is not done until CI has applied the new artifact and the integration tests pass against the deployed Lambda.
      - Integration test: A and B in active room R (friendship_active=true), A blocks B → ChatRooms row shows status=deactivated, deactivated_reason=blocked, deactivated_by=A, friendship_active=false. A sendMessage call by either party returns RoomDeactivated.
      - Integration test: A unblocks B → ChatRooms row shows status=active, deactivated_* attributes removed, reactivated_at set, friendship_active still false. A sendMessage call by either party returns RoomReadOnly.
      - Integration test: A blocks B without any prior chat room → no DynamoDB row exists, UpdateItem no-ops, no error.
      - The actual AppSync onRoomDeactivated / onRoomReactivated subscriber broadcast is delivered by story 8.9a's stream publisher — NOT by this Lambda. This Lambda is DynamoDB-and-Aurora-only.
    notes: "Three explicit deliverables (code, IAM, CI redeploy) preempt the 'permission denied first time someone blocks' trap. The blocks Lambda never calls AppSync directly — the DDB stream publisher in 8.9a handles the broadcast."

  - id: 8.9a
    title: Room-state publisher Lambda (DynamoDB Streams → AppSync publish mutations)
    agent: backenddeveloper
    tracking_issue: 120
    done: false
    depends_on: [8.1, 8.2, 8.9]
    acceptance_criteria:
      - New Lambda infrastructure/src/functions/room_state_publisher/ consumes a DynamoDB Stream on the ChatRooms table (the table's stream_enabled was previously off — this story enables NEW_AND_OLD_IMAGES on ChatRooms in modules/dynamodb/main.tf; phase-2 brainstorm finding #17 noted the table currently lacks a stream)
      - The Lambda inspects each MODIFY event: when status transitions active → deactivated, calls the AppSync `_publishRoomDeactivated(roomId, payload)` mutation via SigV4 (IAM auth mode); when status transitions deactivated → active, calls `_publishRoomReactivated(roomId, payload)`
      - The Lambda is in-VPC ONLY IF it must (AppSync HTTPS reachable via an existing VPC endpoint or via public DNS — confirm during build). If reachable from outside the VPC, the Lambda runs outside the VPC for simplicity. State the chosen placement in handler.py with a one-line WHY.
      - New IAM role room_state_publisher_role in iam_roles module: dynamodb:DescribeStream + GetRecords + GetShardIterator + ListStreams on the ChatRooms stream ARN, and appsync:GraphQL on the relevant publish-mutation field ARNs
      - Event source mapping wires the ChatRooms stream to the Lambda with batch_size=10, starting_position=LATEST
      - Terraform module infrastructure/modules/room_state_publisher/ provisions Lambda + EventSourceMapping; dev and prod root modules wire it
      - Makefile package-all extended with room_state_publisher.zip target
      - Integration test (using a localstack or a stubbed AppSync HTTP target): MODIFY event with status active→deactivated → publisher Lambda invokes the publish mutation with the expected payload exactly once; subscribed AppSync client receives onRoomDeactivated within 3 seconds
      - Integration test: a MODIFY event that does not change status (e.g. last_message_at update) does NOT invoke any publish mutation
    notes: "This is the option (b) middleman pattern from brainstorm finding #2 — keeps the blocks Lambda DynamoDB-only and centralizes AppSync publishing in one place."

  - id: 8.9b
    title: Extend the knotify-friends Lambda to maintain ChatRooms.friendship_active on accept AND unfriend
    agent: backenddeveloper
    tracking_issue: 121
    done: false
    depends_on: [8.9]
    acceptance_criteria:
      - 1. CODE EDIT (infrastructure/src/functions/friends/handler.py)
          - On friend-request-accept (the existing `_handle_accept_friend_request` handler from phase 6), AFTER the Aurora INSERT INTO friendships, perform a DynamoDB UpdateItem on ChatRooms(room_id = knotify_obs.chat_room_id(from_user, to_user)) setting friendship_active=true. The UpdateItem is conditional on attribute_exists(room_id) — if no chat room ever existed yet, this no-ops (the next createOrGetRoom in story 8.3 will set the flag to true at row-creation time).
          - On unfriend (the existing `_handle_delete_friend` handler from phase 6, DELETE /v1/friends/{userId}), AFTER the Aurora DELETE FROM friendships, perform a DynamoDB UpdateItem on ChatRooms(room_id = knotify_obs.chat_room_id(current_user, target_user)) setting friendship_active=false. Same attribute_exists(room_id) condition — no-ops if no chat room ever existed. The room's `status` stays whatever it was (typically 'active'); only the flag flips, so the room becomes read-only by the same gate that 8.4 enforces.
      - 2. IAM POLICY UPDATE (infrastructure/modules/iam_roles/)
          - The role attached to the friends Lambda gains scoped dynamodb:UpdateItem on the ChatRooms table ARN.
      - 3. REDEPLOY VIA CI (per the same rule as 8.9 step 3)
      - Integration test (accept-path, refriend after unblock): A and B were friends and chatted, A blocked B then unblocked B (room is now status=active, friendship_active=false), B sends A a friend request and A accepts → ChatRooms.friendship_active flips to true; subsequent sendMessage by either party succeeds (no longer returns RoomReadOnly).
      - Integration test (accept-path, no prior room): A and B were never friends and never chatted, A sends B a friend request and B accepts → friend row created in Aurora, UpdateItem on the non-existent ChatRooms row no-ops, no error.
      - Integration test (unfriend-path): A and B are friends with an active chat room (status=active, friendship_active=true). A calls DELETE /v1/friends/B → friendship row removed in Aurora, ChatRooms.friendship_active flips to false, ChatRooms.status stays 'active'. Subsequent sendMessage by either party returns RoomReadOnly.
      - Integration test (unfriend-path, no prior room): A and B are friends but never chatted. A calls DELETE /v1/friends/B → friendship row removed, UpdateItem on the non-existent ChatRooms row no-ops, no error.
    notes: "Completes the friendship_active flag lifecycle introduced in 8.4 and 8.9: accept flips true, unfriend flips false. Same code-IAM-CI three-step structure as 8.9 to preempt the same permission-denied trap."

  - id: 8.9c
    title: Notifications-stream publisher Lambda (DynamoDB Streams → AppSync publishNotification / _publishFriendRequestUpdated)
    agent: backenddeveloper
    tracking_issue: 122
    done: false
    depends_on: [8.1, 8.2]
    acceptance_criteria:
      - New Lambda infrastructure/src/functions/notifications_publisher/ consumes the existing Notifications DynamoDB Stream (phase 2 already provisioned `stream_enabled = true` + `stream_view_type = "NEW_IMAGE"` on the Notifications table at modules/dynamodb/main.tf:253-254 for PushFanout consumption — this story does NOT modify the DDB module, it only adds a second event source mapping)
      - The Lambda inspects each INSERT event and dispatches by `type`. In both branches below, the payload forwarded to the AppSync mutation MUST include the recipient's `user_id` field (sourced from the Notifications row's PK, which is the recipient's Cognito sub) — the subscription pipeline resolvers in story 8.6 filter on `user_id == identity.sub` so without it identity-scoped delivery breaks:
          - type `friend_request_received`, `bookmark`, `match`, or any other generic notification → calls AppSync `publishNotification(notification: <payload>)` via SigV4 (IAM auth mode); `notification.user_id` is what onNotificationForMe filters by
          - type `friend_request_accepted` (or whatever the phase-6 friends-accept handler writes) → calls AppSync `_publishFriendRequestUpdated(payload)` via SigV4; `payload.user_id` is what onFriendRequestUpdated filters by
          - Unknown/future types → log a warning, do nothing (don't fail the batch)
      - The Lambda runs OUTSIDE the VPC (AppSync HTTPS over public DNS; same rationale as 8.9a)
      - New IAM role notifications_publisher_role in iam_roles module: dynamodb:DescribeStream + GetRecords + GetShardIterator + ListStreams on the Notifications stream ARN, appsync:GraphQL on the publishNotification and _publishFriendRequestUpdated field ARNs
      - Event source mapping wires the Notifications stream to the Lambda with batch_size=10, starting_position=LATEST
      - Terraform module infrastructure/modules/notifications_publisher/ provisions Lambda + EventSourceMapping; dev and prod root modules wire it
      - Makefile package-all extended with notifications_publisher.zip target
      - Integration test (using a stubbed AppSync HTTP target): INSERT a Notifications row of type=friend_request_received → publisher invokes publishNotification exactly once with the row payload; a connected onNotificationForMe subscriber whose identity.sub matches notification.user_id receives the event within 3 seconds
      - Integration test: INSERT a Notifications row of type=friend_request_accepted → publisher invokes _publishFriendRequestUpdated exactly once; a connected onFriendRequestUpdated subscriber receives it within 3 seconds
      - Integration test: MODIFY events on Notifications (e.g. delivered=true updates written by PushFanout in 8.10) do NOT trigger another publish call (publisher only acts on INSERT)
    notes: "Closes the gap surfaced in re-run brainstorm finding B — schema in 8.2 declares publishNotification and _publishFriendRequestUpdated, but nothing was wired to invoke them. This is the option-(b) DDB-streams middleman pattern applied to the Notifications table (8.9a applies the same pattern to ChatRooms). CONSUMER LIMIT: with this story's ESM the Notifications stream has 2 ESM consumers (notifications_publisher + push_fanout from 8.10), which is at the AWS default limit of 2 simultaneous consumers per DynamoDB stream. A third consumer would require switching to Kinesis Data Streams for DynamoDB or a fan-out Lambda — flag at design time, not at implementation time."

  - id: 8.10
    title: PushFanout Lambda triggered by DynamoDB Streams
    agent: backenddeveloper
    tracking_issue: 123
    done: false
    depends_on: [8.4]
    acceptance_criteria:
      - infrastructure/src/functions/push_fanout/ Lambda receives DynamoDB stream events from BOTH ChatMessages and Notifications (two event source mappings, same Lambda function)
      - Lambda runs OUTSIDE the VPC (no VPC config). Justification (recorded in handler.py one-line comment): only touches DynamoDB (no VPC required) and Expo (open internet). Inside-VPC placement would repeat hotfix #106's blackhole.
      - For ChatMessages INSERT: GetItem ChatRooms(room_id) to determine recipient (whichever of user_a/user_b is not sender_id), GetItem ChatRoomMembership(recipient, room_id) to check notifications_muted, Query PushNotificationTokens with PK=recipient, then POST to Expo for each token. Payload: title = sender's display name (sourced from ChatRoomMembership.cached_other_name flipped — i.e. recipient's row caches sender's name), body = first 80 chars of message content, deep link = `knotify://chat/<room_id>`
      - For Notifications INSERT: recipient = item.user_id, Query PushNotificationTokens with PK=recipient, POST to Expo. On HTTP 200, UpdateItem Notifications SET delivered=true
      - Expo "DeviceNotRegistered" response → DeleteItem PushNotificationTokens(user_id, device_id) for the offending token
      - Expo URL sourced from an environment variable EXPO_PUSH_URL so unit tests can target a mock
      - Expo authentication:
          - DEV: unauthenticated mode (no access token header). Acceptable for dev rate limits.
          - PROD: access token stored in Secrets Manager as `knotify-prod-expo-push-credential`; Lambda reads on cold start; passed as `Authorization: Bearer <token>` header.
          - Distinguished via an `EXPO_AUTH_MODE` environment variable (`none` | `bearer`) set per environment.
      - New IAM role push_fanout_role in iam_roles module: dynamodb:GetItem + Query + UpdateItem + DeleteItem on the four touched tables (ChatRooms, ChatRoomMembership, Notifications, PushNotificationTokens), dynamodb stream actions on the ChatMessages and Notifications stream ARNs, scoped secretsmanager:GetSecretValue on the Expo prod secret (prod only)
      - Terraform module infrastructure/modules/push_fanout/ provisions Lambda + two EventSourceMappings (batch_size=10, starting_position=LATEST); dev and prod root modules wire it
      - Makefile package-all extended with push_fanout.zip target
      - Integration test (against a local mock of the Expo API): insert a ChatMessages row, observe the mock receives a payload with the expected title, body, and deep link; assert NO Notifications row is created (chat messages do not write to Notifications, per §5.4.2)
      - Integration test: insert a ChatMessages row whose recipient's ChatRoomMembership.notifications_muted=true → no Expo call made
      - Integration test: Expo mock returns DeviceNotRegistered → PushNotificationTokens row is deleted
    notes: ""

  - id: 8.11
    title: POST /v1/push-tokens REST endpoint
    agent: backenddeveloper
    tracking_issue: 124
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/src/functions/push_tokens/ implements POST /v1/push-tokens with JSON body {platform, push_token, device_id, app_version}; PutItem (which acts as upsert) on PushNotificationTokens with PK=identity.sub, SK=device_id, attributes push_token + platform + app_version + last_seen=NOW()
      - NOT gated by @require_profile_complete — tokens register on app first-launch before onboarding completes
      - JWT authorizer enforced by the existing HTTP API Cognito authorizer
      - Route wiring (hotfix #86 lesson — be explicit):
          - aws_apigatewayv2_integration.push_tokens
          - aws_apigatewayv2_route.post_push_tokens (POST /v1/push-tokens) with authorization_type=JWT and authorizer_id=module.api_gateway.authorizer_id
          - aws_lambda_permission.push_tokens_api_gateway with `source_arn = module.api_gateway.api_execution_arn` — NOT default_stage_arn. Using default_stage_arn returns API Gateway 5xx with no Lambda invocation log, exactly as hotfix #86 documented.
      - test_route_wiring.py `_EXPECTED_ROUTE_KEYS` extended from 19 to 20 to include POST /v1/push-tokens
      - IAM role push_tokens_role in iam_roles module: dynamodb:PutItem on PushNotificationTokens
      - Terraform module infrastructure/modules/push_tokens/; dev and prod root modules wire it
      - Makefile package-all extended with push_tokens.zip target
      - Integration test: signup a user, POST a token, GET via internal Lambda invocation returns the upserted item
      - Integration test: POST again with the same device_id updates last_seen rather than creating a duplicate (single row per (user_id, device_id))
    notes: ""

  - id: 8.12
    title: Stale token cleanup scheduled Lambda
    agent: backenddeveloper
    tracking_issue: 125
    done: false
    depends_on: [8.11]
    acceptance_criteria:
      - infrastructure/src/functions/stale_token_cleanup/ Lambda is invoked by an aws_cloudwatch_event_rule daily; scans PushNotificationTokens, deletes rows whose last_seen is older than 60 days
      - IAM role with dynamodb:Scan + DeleteItem on PushNotificationTokens
      - Lambda runs outside the VPC (DynamoDB-only)
      - Terraform module infrastructure/modules/stale_token_cleanup/; dev and prod root modules wire it
      - Makefile package-all extended with stale_token_cleanup.zip target
      - Integration test: seed a token with last_seen 61 days ago, invoke the Lambda, assert the row is gone; seed one with last_seen 59 days ago, assert it remains
    notes: ""

  - id: 8.13
    title: End-to-end chat test
    agent: backenddeveloper
    tracking_issue: 126
    done: false
    depends_on: [8.4, 8.6, 8.7, 8.9, 8.9a, 8.9b, 8.9c, 8.10, 8.11]
    acceptance_criteria:
      - tests/integration/chat_e2e_test.py provisions two test users via Cognito, completes both profiles (so custom:profile_complete claim flips true), has them become friends via the phase-6 friends API
      - Both users POST /v1/push-tokens with a fixture token before any sendMessage call (this is what makes the Expo mock assertion below verifiable)
      - Opens an AppSync subscription on B's behalf to onMessageInRoom for the canonical room
      - A calls createOrGetRoom, then sendMessage("hello"). The mutation takes no client idempotency argument — the resolver derives the ClientRequestToken server-side from (sender, room, content, second-precision timestamp).
      - Subscription receives the message within 3 seconds
      - Expo Push mock (configured via the EXPO_PUSH_URL env var) receives the corresponding push call within 5 seconds, with the fixture push_token in the payload
      - Idempotency is proven by the story 8.4 unit test of the token-derivation function (deployed-Lambda clock cannot be pinned, so no E2E replay assertion here). The E2E only asserts that the single sendMessage call above produced exactly one ChatMessages row.
      - A sendMessage with a foreign room_id (one A is not in) is rejected with Unauthorized
      - A blocks B → both clients receive onRoomDeactivated (via 8.9a publisher) within 3 seconds; B's subsequent sendMessage fails with RoomDeactivated
      - A unblocks B → both clients receive onRoomReactivated; B's subsequent sendMessage fails with RoomReadOnly (friendship still gone)
      - B sends A a friend request and A accepts → ChatRooms.friendship_active flips true (story 8.9b); subsequent sendMessage by either party succeeds
    notes: ""
