phase: 8
title: Chat (AppSync + DynamoDB Streams + push fan-out)
last_updated: 2026-06-10

context_summary: |
  Delivers the full chat capability in a single phase per the owner's resolved Option A: the AppSync GraphQL API with a hand-written schema (no Amplify auto-generation, no auto-CRUD subscriptions), pipeline resolvers that enforce membership and block checks against Aurora before establishing subscriptions, the deterministic-room-id creation flow from §5.4.1, DynamoDB Streams from ChatMessages and Notifications wired to a PushFanout Lambda that targets Expo Push (per the §13 #7 resolution in v1.6), the POST /v1/push-tokens REST endpoint for token registration, and the stale-token cleanup scheduled Lambda. This phase intentionally ships data plane and API plane together because the GraphQL schema and the DynamoDB key design are tightly coupled. After this phase only account deletion, observability consolidation, hardening, and S3 photos remain.

## Carryovers from phase 6
- Chat-room-without-backing-friendship is read-only — phase-6 story 6.4 deletes the friendship row on POST /v1/blocks and reactivates the chat room on DELETE /v1/blocks, but does NOT recreate the friendship on unblock. Phase 8's chat-write path must treat an `active` ChatRooms row whose canonical pair has no `friendships` entry as read-only. Source: phase-6 third-pass Md1.

stories:
  - id: 8.1
    title: AppSync API Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/appsync/main.tf creates aws_appsync_graphql_api with authentication_type AMAZON_COGNITO_USER_POOLS (primary) and additional_authentication_provider AWS_IAM (secondary for backend), referencing the Cognito User Pool from phase 4
      - log_config writes to CloudWatch at FIELD level (cloudwatch_logs_role_arn provided) with the standard 7-day retention applied via the lambda module pattern
      - Data sources are declared for the DynamoDB tables (ChatRooms, ChatRoomMembership, ChatMessages, MessageReads, Notifications) and one Lambda data source for cross-Aurora calls (friendship/block checks)
      - Module outputs api_id, graphql_url, realtime_url
    notes: ""

  - id: 8.2
    title: Hand-written GraphQL schema
    agent: backenddeveloper
    done: false
    depends_on: [8.1]
    acceptance_criteria:
      - File infrastructure/modules/appsync/schema.graphql defines Message, ChatRoom, ChatRoomMembership, MessageRead, Notification, TypingEvent types with the field shapes from §5.4 of architecture.md
      - The Subscription type declares ONLY scoped subscriptions: onMessageInRoom(roomId: ID!), onNotificationForMe, onTypingInRoom(roomId: ID!), onRoomDeactivated(roomId: ID!), onFriendRequestUpdated — and each is annotated with @aws_subscribe pointing at the correct mutation
      - The schema contains no auto-generated onCreateMessage/onUpdateMessage/onDeleteMessage subscriptions
      - The sendMessage mutation signature has no senderId argument; sender is derived server-side
      - aws_appsync_graphql_api.schema points at this file; terraform apply succeeds and the introspection query returns the declared schema
    notes: ""

  - id: 8.3
    title: createOrGetRoom resolver (idempotent room creation)
    agent: backenddeveloper
    done: false
    depends_on: [8.1, 8.2]
    acceptance_criteria:
      - A Lambda resolver implements the §5.4.1 createOrGetRoom flow: verifies caller != other user, queries Aurora for friendship (both directions) and block (both directions) via the Lambda data source, computes room_id=sha256(canonical_pair(caller, other)), attempts ChatRooms PutItem with attribute_not_exists(room_id), and on conditional failure proceeds without writing
      - On successful creation, inserts both ChatRoomMembership rows in a TransactWrite with the ChatRooms put
      - Integration test: A and B are friends, A calls createOrGetRoom(B) twice → same room_id, exactly one ChatRooms row, exactly two ChatRoomMembership rows
      - Integration test: A and C are not friends → createOrGetRoom returns Unauthorized with reason NOT_FRIENDS
      - Integration test: A has blocked B → returns Unauthorized with reason BLOCKED
    notes: ""

  - id: 8.4
    title: sendMessage resolver
    agent: backenddeveloper
    done: false
    depends_on: [8.3]
    acceptance_criteria:
      - Lambda resolver for sendMessage: GetItem ChatRoomMembership(sender, roomId), reject if missing; GetItem ChatRooms(roomId), reject if status != 'active'; insert ChatMessages with server-set sender_id, delivered_at=NOW(), and SK=<iso-timestamp>#<ulid>; update ChatRooms last_message fields in the same TransactWrite
      - sender_id is never read from the GraphQL arguments
      - Integration test: A sends a message in a room A and B share → ChatMessages row appears with sender_id=A, content_type=text, delivered_at within 1 second of the call
      - Integration test: a user not in the room attempts sendMessage with the room_id → resolver returns Unauthorized
      - Integration test: room is deactivated → sendMessage returns RoomDeactivated
    notes: ""

  - id: 8.5
    title: Query resolvers listMyRooms and messagesByChatRoom
    agent: backenddeveloper
    done: false
    depends_on: [8.3]
    acceptance_criteria:
      - listMyRooms: DynamoDB Query on ChatRoomMembership with PK=identity.sub, returns the list ordered by ChatRooms.last_message_at (fetched in a parallel BatchGet)
      - messagesByChatRoom(roomId, createdAtGt?, limit?): membership check then Query on ChatMessages PK=roomId, SK > <createdAtGt>, descending, paginated via nextToken
      - Integration test: messagesByChatRoom returns the latest 20 messages by default; passing createdAtGt fetches only newer messages
    notes: ""

  - id: 8.6
    title: Scoped subscriptions with pipeline membership check
    agent: backenddeveloper
    done: false
    depends_on: [8.3]
    acceptance_criteria:
      - Each subscription (onMessageInRoom, onTypingInRoom, onRoomDeactivated) is backed by a pipeline resolver whose first function checks ChatRoomMembership(identity.sub, roomId); on miss the resolver returns Unauthorized and the WebSocket subscription fails to establish
      - onNotificationForMe is identity-scoped: filter expression user_id == identity.sub
      - Integration test: a user not in room R attempts subscription onMessageInRoom(R) → connection rejected before any message can be received
      - Integration test: A and B in room R, A sends a message → B's onMessageInRoom subscription receives the Message within 2 seconds
    notes: ""

  - id: 8.7
    title: markAsRead mutation and read-receipt updates
    agent: backenddeveloper
    done: false
    depends_on: [8.4]
    acceptance_criteria:
      - markAsRead(roomId, lastMessageId): TransactWrite updates MessageReads (PK roomId, SK identity.sub) with last_read_message_id+last_read_at and ChatRoomMembership(identity.sub, roomId) with the same cached values
      - An onReadReceipt subscription (scoped to roomId via membership check) delivers the read receipt to the other participant
      - Integration test: A sends message m1, B calls markAsRead(R, m1) → MessageReads has B's row with last_read_message_id=m1; A's onReadReceipt subscription receives the event
    notes: ""

  - id: 8.8
    title: setTyping mutation with no storage
    agent: backenddeveloper
    done: false
    depends_on: [8.6]
    acceptance_criteria:
      - setTyping(roomId, isTyping) uses an AppSync None data source; the resolver validates membership and returns the payload to be fanned out via onTypingInRoom
      - No DynamoDB write occurs (verified by examining a CloudTrail trace of the mutation call)
      - Integration test: A calls setTyping(R, true) → B's onTypingInRoom subscription receives {userId: A, isTyping: true}
    notes: ""

  - id: 8.9
    title: Room deactivation and reactivation flow
    agent: backenddeveloper
    done: false
    depends_on: [8.3]
    acceptance_criteria:
      - The knotify-blocks Lambda from phase 6 is extended so a block also performs a DynamoDB UpdateItem on the matching ChatRooms row: status='deactivated', deactivated_reason='blocked', deactivated_by=blocker, deactivated_at=NOW()
      - The knotify-blocks unblock path performs the conditional UpdateItem from §5.4.1 that reactivates only when deactivated_reason='blocked' AND deactivated_by=current_user
      - onRoomDeactivated subscription fires with the ChatRoom payload when status transitions
      - Integration test: A and B in room R, A blocks B → both clients receive onRoomDeactivated, sendMessage in R now returns RoomDeactivated. A unblocks B → onRoomReactivated fires, sendMessage succeeds again
    notes: ""

  - id: 8.10
    title: PushFanout Lambda triggered by DynamoDB Streams
    agent: backenddeveloper
    done: false
    depends_on: [8.4]
    acceptance_criteria:
      - src/functions/push_fanout/ Lambda receives DynamoDB stream events from BOTH ChatMessages and Notifications (two event source mappings, same Lambda)
      - For ChatMessages INSERT: GetItem ChatRooms(room_id) to determine recipient (whichever of user_a/user_b is not sender_id), GetItem ChatRoomMembership(recipient, room_id) to check notifications_muted, fetch PushNotificationTokens(recipient), call Expo Push HTTP API (https://exp.host/--/api/v2/push/send) for each token
      - For Notifications INSERT: recipient = item.user_id, look up tokens, call Expo, on success UpdateItem Notifications set delivered=true
      - DeviceNotRegistered response from Expo causes the offending token row to be deleted from PushNotificationTokens
      - The Expo URL is sourced from an environment variable so unit tests can target a mock
      - Integration test (against a local mock of the Expo API): insert a ChatMessages row, observe the mock receives a payload with the expected title and deep link, no Notifications row is created (chat messages do not write to Notifications)
    notes: ""

  - id: 8.11
    title: POST /v1/push-tokens REST endpoint
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - src/functions/push_tokens/ implements POST /v1/push-tokens with body {platform, push_token, device_id, app_version}; upserts the PushNotificationTokens row keyed by (user_id, device_id) with last_seen=NOW()
      - Route registered on HTTP API with Cognito JWT authorizer
      - Integration test: signup a user, POST a token, GET via internal Lambda invocation returns the upserted item; POST again with the same device_id updates last_seen rather than creating a duplicate
    notes: ""

  - id: 8.12
    title: Stale token cleanup scheduled Lambda
    agent: backenddeveloper
    done: false
    depends_on: [8.11]
    acceptance_criteria:
      - src/functions/stale_token_cleanup/ Lambda is invoked by an aws_cloudwatch_event_rule daily; scans PushNotificationTokens, deletes rows whose last_seen is older than 60 days
      - Integration test: seed a token with last_seen 61 days ago, invoke the Lambda, assert the row is gone; seed one with last_seen 59 days ago, assert it remains
    notes: ""

  - id: 8.13
    title: End-to-end chat test
    agent: backenddeveloper
    done: false
    depends_on: [8.4, 8.6, 8.7, 8.9, 8.10, 8.11]
    acceptance_criteria:
      - tests/integration/chat_e2e_test.py provisions two test users via Cognito, has them become friends via the phase-6 friends API, opens an AppSync subscription on B's behalf to onMessageInRoom for the canonical room, has A call createOrGetRoom then sendMessage("hello")
      - The subscription receives the message within 3 seconds
      - The Expo Push mock (configured via env var) receives the corresponding push call within 5 seconds
      - A second sendMessage with a foreign room_id (one A is not in) is rejected
      - A blocks B → both clients receive onRoomDeactivated, B's subsequent sendMessage fails, A unblocks B and sendMessage succeeds again
    notes: ""
