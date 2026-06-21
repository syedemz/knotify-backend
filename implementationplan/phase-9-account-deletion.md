phase: 9
title: Account deletion (Step Functions, soft delete)
last_updated: 2026-06-20 (story 9.10)

context_summary: |
  Implements the account-deletion workflow per §11 of architecture.md with the §13 #8 resolution applied: soft delete (UPDATE users SET deleted_at, strip PII) with a 30-day retention before a scheduled hard purge via cascade. ChatMessages are anonymized rather than deleted per §13 #21 — sender_id rewritten to '[deleted-user]' while content is preserved. Step Functions Standard workflow orchestrates the steps; each step is an idempotent Python 3.14 Lambda. An audit log table records initiation and completion. A purge_immediately flag supports GDPR right-to-be-forgotten by branching at workflow entry into a hard-delete path that fully removes the requester's Aurora rows, ChatMessages, and ChatRoomMembership rows. This phase ships after chat because the workflow needs to deactivate ChatRooms, anonymize/hard-delete ChatMessages, and clean ChatRoomMembership — all DynamoDB tables created in phase 2 and operated on by phase 8.

  Phase-wide acceptance: every Lambda below has a dedicated least-privilege IAM role declared in infrastructure/modules/iam_roles/. Aurora-touching Lambdas (9.5, 9.11, 9.12) reuse the existing db layer at infrastructure/src/layers/db/ and read credentials from the existing Secrets Manager rotation pattern (no new secret machinery). Every step Lambda is idempotent on empty inputs — a user with zero chats / zero notifications / zero tokens / zero friends must succeed-as-noop at every step. All TF tests green at phase close.

stories:
  - id: 9.1
    title: Step Functions state machine module
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 135
    acceptance_criteria:
      - infrastructure/modules/step_functions/main.tf declares aws_sfn_state_machine of type STANDARD with the definition expressed as a templated JSON file
      - ValidateDeletionRequest (9.2) is wired with `ResultPath: '$.validation'` so the original input (including `purge_immediately` and `user_id`) is preserved past this task and remains addressable by the downstream Choice and by every subsequent task
      - The state machine forks at a top-level Choice on `$.purge_immediately` immediately after ValidateDeletionRequest
      - Soft-delete branch (purge_immediately=false) — DisableCognitoUser(mode=disable) → DeactivateChatRooms → ParallelCleanup{SoftDeleteAurora, DeleteDynamoDBPersonalData, AnonymizeChatMessages} → DeleteCognitoUser(mode=delete) → WriteAuditLog
      - Purge-immediately branch (purge_immediately=true) — DisableCognitoUser(mode=disable) → DeactivateChatRooms → ParallelCleanup{SoftDeleteAurora → HardPurgeNow, DeleteDynamoDBPersonalData, HardDeleteUserChatMessages} → DeleteCognitoUser(mode=delete) → WriteAuditLog
      - DeactivateChatRooms (9.4) is wired with `ResultPath: '$.deactivate_chat_rooms'` so its result lands at a known path. ParallelCleanup uses an explicit Step Functions `Parameters` block to inject `{user_id.$: '$.user_id', room_ids.$: '$.deactivate_chat_rooms.room_ids'}` into AnonymizeChatMessages (soft-delete branch) and HardDeleteUserChatMessages (purge_immediately branch). Those Lambdas MUST receive room_ids as input — they must not Query ChatRoomMembership themselves (the rows are gone by then)
      - DisableCognitoUser and DeleteCognitoUser task definitions each use a Parameters block to inject `{user_id.$: '$.user_id', mode: 'disable'}` or `mode: 'delete'` respectively (mode value is task-local, not derived from state)
      - Lambda ARNs are declared as module input variables (e.g. `var.lambda_arns.validate_deletion_request`, `var.lambda_arns.cognito_user_state`, etc.); the root TF in environments/ supplies the actual ARNs from each Lambda module's outputs after those Lambdas are deployed. Module-level TF tests pass with stub ARN strings — end-to-end correctness of the wired state machine is validated in story 9.13
      - Both branches share a single global Catch that writes a "deletion_failed" audit row (reusing the WriteAuditLog Lambda from 9.8 with an event_type parameter) and emits a CloudWatch custom metric DeletionFailed (Count=1, dimensions=stage). No SNS topic is provisioned in this phase; the ops email subscription on the metric is wired in phase 10 observability work
      - No NotifySuccess step is provisioned (SES/email infrastructure does not exist in phase 9; deferred)
      - Each task references the matching Lambda ARN from the stories below
      - logging_configuration enabled at ALL with the 7-day retention CloudWatch log group
      - A dedicated Step Functions execution role is declared in infrastructure/modules/iam_roles/ with lambda:InvokeFunction scoped to each task Lambda ARN, cloudwatch:PutMetricData for the DeletionFailed metric, and logs:* for the log group
      - Module output: state_machine_arn
    notes: ""

  - id: 9.2
    title: ValidateDeletionRequest Lambda
    agent: backenddeveloper
    done: true
    depends_on: [9.8]
    tracking_issue: 136
    acceptance_criteria:
      - Lambda confirms the input user_id matches the JWT sub passed via input, checks the audit table for an in-progress deletion for the same user_id (idempotency), writes a "deletion_initiated" audit record
      - The Lambda's response is a small validation summary (e.g., `{validated: true, audit_event_id: ...}`); the state-machine wiring in 9.1 uses `ResultPath: '$.validation'` so the original input fields (`user_id`, `purge_immediately`) survive unchanged and remain available to the downstream Choice and all subsequent tasks. The Lambda does NOT need to echo input fields back
      - Integration test: a second invocation for the same user_id while a prior execution is still in-progress returns a "DeletionInProgress" error state for Step Functions
    notes: ""

  - id: 9.3
    title: Cognito user-state Lambda (disable + delete modes)
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 137
    acceptance_criteria:
      - Single Lambda accepts an input field `mode` with values "disable" | "delete" and dispatches to cognito-idp AdminDisableUser or AdminDeleteUser respectively for the user_id
      - Idempotent for both modes: catches UserNotFoundException and already-disabled cases and treats them as success; "delete" on a missing user is a no-op success
      - The state machine in 9.1 wires this Lambda twice: as DisableCognitoUser early (mode=disable) and as DeleteCognitoUser late (mode=delete)
      - Integration test: invoke with mode=disable on a fresh Cognito user, then mode=disable again — both succeed; then invoke with mode=delete, assert AdminGetUser returns UserNotFoundException; invoke mode=delete again — succeeds (idempotent)
    notes: ""

  - id: 9.4
    title: DeactivateChatRooms Lambda (also clears deleted user's ChatRoomMembership)
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 138
    acceptance_criteria:
      - Lambda queries ChatRoomMembership PK=user_id for all room_ids, then UpdateItem on each ChatRooms row to set status='deactivated', deactivated_reason='user_deleted_account', deactivated_at=NOW()
      - After deactivating the ChatRooms rows, the Lambda BatchWriteItem-deletes the deleted user's ChatRoomMembership rows (PK=user_id, SK=room_id for each room_id collected above). Surviving participants' membership rows are NOT touched
      - The Lambda's response includes `{room_ids: [list of room_ids the user was in]}` so the state-machine Parameters block in 9.1 can inject this list into downstream tasks (AnonymizeChatMessages, HardDeleteUserChatMessages) that need it AFTER the membership rows have been deleted
      - AppSync onRoomDeactivated publishes are NOT issued from this Lambda — phase-8 story 8.9a (room_state_publisher) already consumes the ChatRooms DDB stream and publishes the mutation. This story only writes to DynamoDB
      - Conditional update on ChatRooms skips rows already deactivated for any reason (idempotent); membership deletes are unconditional but tolerate already-deleted rows
      - Integration test: user with 3 rooms — one already deactivated by block — invoke the Lambda; afterwards all 3 ChatRooms rows are deactivated; the one previously blocked keeps deactivated_reason='blocked' (most-recent-wins is NOT used; reason of first deactivation persists); the deleted user's ChatRoomMembership rows for all 3 rooms are gone; the surviving participants' rows for those 3 rooms are still present; the response payload contains the 3 room_ids
    notes: ""

  - id: 9.5
    title: SoftDeleteAurora Lambda
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 139
    acceptance_criteria:
      - Lambda runs the §11.1 step-2 SQL: UPDATE users SET deleted_at=NOW(), email=NULL, phone_number=NULL, photo_url=NULL, chosen_profile_avatar=NULL, preferences='{}', preference_vector=NULL, username='[deleted-user]', first_name='Deleted', last_name='User' WHERE user_id=:id AND deleted_at IS NULL
      - Uses the existing db layer at infrastructure/src/layers/db/ for psycopg; reads Aurora credentials from the existing Secrets Manager pattern used by other aurora-writer Lambdas (no new secret machinery)
      - The UPDATE is conditional on deleted_at IS NULL so re-runs are no-ops (idempotent — repeat invocations return success with rows_affected=0)
      - Integration test: invoke on a fresh user, assert the row has deleted_at set and PII fields nulled, AND the friendships rows still exist; invoke again, no change
    notes: ""

  - id: 9.6
    title: AnonymizeChatMessages Lambda
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 140
    acceptance_criteria:
      - Lambda receives `{user_id, room_ids: [...]}` as input (room_ids supplied by the state-machine Parameters block from 9.4's output — see 9.1). For each room_id, Query ChatMessages by room_id with a FilterExpression on sender_id, and UpdateItem each matching row to set sender_id='[deleted-user]' while preserving content. No new GSI is added to ChatMessages — cost is linear in messages-in-rooms-the-user-was-in, not total chat traffic
      - The Lambda does NOT Query ChatRoomMembership itself — by the time it runs (inside ParallelCleanup, after DeactivateChatRooms), those membership rows have been deleted. The room_ids list is the authoritative input
      - The handler accepts and returns a continuation token containing `{room_ids, current_room_index, last_evaluated_key}`. Step Functions iterates via a Choice → Task → Choice loop: the Choice inspects an `has_more` flag in the output; if true, loops back to the Task with the returned continuation token; if false, exits the loop
      - In-Lambda paginated work is bounded by the 15-minute Lambda timeout; for users whose work cannot complete in one invocation, the continuation token survives and Step Functions re-invokes
      - Integration test: seed a user with 25 messages across 3 rooms, invoke with `{user_id, room_ids: [r1, r2, r3]}` (single call), assert all 25 rows have sender_id='[deleted-user]' and content is unchanged
      - Integration test (empty input): invoke with `{user_id, room_ids: []}`; assert success with has_more=false on first call
    notes: ""

  - id: 9.7
    title: DeleteDynamoDBPersonalData Lambda
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 141
    acceptance_criteria:
      - Lambda deletes all Notifications rows where user_id=:id (Query then BatchWriteItem deletes in batches of 25) and all PushNotificationTokens rows where user_id=:id
      - Idempotent: re-running on an empty result set returns success
      - Integration test: seed 30 notifications and 2 push tokens for a user, invoke, assert zero remain
    notes: ""

  - id: 9.8
    title: Audit log table and WriteAuditLog Lambda
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 142
    acceptance_criteria:
      - A new DynamoDB table account_deletion_audit (PK user_id, SK event_id) is added to infrastructure/modules/dynamodb/ (same module that owns every other table)
      - DynamoDB TTL configured with attribute `expire_at` of type Number (Unix epoch seconds, NOT an ISO string). The writer Lambda computes `expire_at = int(time.time()) + 7*365*86400` per row
      - The Lambda accepts an `event_type` input ("deletion_initiated" | "deletion_completed" | "deletion_failed") and writes the corresponding record. The "deletion_completed" record includes `branches_succeeded` list, `completed_at`, `execution_arn`, and a `dynamodb_retention` field whose value is "permanent_anonymized" on the soft-delete branch or "hard_deleted" on the purge_immediately branch (so compliance can see the retention policy at the row level)
      - The "deletion_failed" record (written from the state machine's global Catch in 9.1) includes `failed_state_name`, `error`, `cause`, and `execution_arn`
      - Integration test: drive a full soft-delete execution and assert both "deletion_initiated" and "deletion_completed" rows exist with dynamodb_retention="permanent_anonymized"; drive a purge_immediately=true execution and assert the "deletion_completed" row has dynamodb_retention="hard_deleted"
    notes: ""

  - id: 9.9
    title: knotify-deletion-initiator Lambda and DELETE /v1/profile/me route
    agent: backenddeveloper
    done: true
    depends_on: [9.1]
    tracking_issue: 143
    acceptance_criteria:
      - infrastructure/src/functions/deletion_initiator/ Lambda extracts user_id from the JWT sub, calls states StartExecution on the state machine from 9.1 with input {user_id, purge_immediately: <body flag, default false>}, returns 202 with executionArn
      - Route DELETE /v1/profile/me wired on HTTP API with Cognito JWT authorizer. The route is wired as a separate aws_apigatewayv2_integration pointing at the deletion_initiator Lambda; the existing `profile` Lambda (which serves GET and PUT on the same path) is unaffected
      - Sanity-check during implementation: the CloudFront-fronted WAF must not block a bodyless DELETE request to this route. No WAF rule change is expected, but confirm by issuing a DELETE through the CloudFront distribution in the integration test (not directly against API Gateway)
      - Integration test (API-level only — full-workflow assertion lives in 9.13): signup a user, call DELETE /v1/profile/me through CloudFront, assert 202 response with a valid executionArn in the body, then call ListExecutions on the state machine and assert an execution exists for the returned executionArn (status may be RUNNING, SUCCEEDED, or FAILED — only existence is asserted here). Do NOT poll for SUCCEEDED in this story
    notes: ""

  - id: 9.10
    title: GET /v1/profile/me/deletion-status endpoint
    agent: backenddeveloper
    done: true
    depends_on: [9.9]
    tracking_issue: 144
    acceptance_criteria:
      - The knotify-deletion-initiator Lambda also handles GET /v1/profile/me/deletion-status?executionArn=<arn> by calling DescribeExecution and returning status, startDate, stopDate, and the names of completed steps
      - Authorization: the handler calls DescribeExecution, parses the returned `execution.input` field as JSON, and asserts `input.user_id == jwt.sub`. If they do not match, the handler returns 403 with NO execution metadata in the response body (do not leak status/timestamps to an attacker probing for valid executionArns)
      - Integration test (API-level only — full-workflow polling assertion lives in 9.13): start a minimal Step Functions execution (either a stub no-op state machine deployed for this test, or reuse an execution created by 9.9's test) and call GET /v1/profile/me/deletion-status?executionArn=<arn> with the owning user's JWT; assert 200 response with a body containing status, startDate, and the names list. Negative test (primary assertion of this story): signup a second user, attempt to read the first user's executionArn with the second user's JWT, assert 403 and an empty body
    notes: ""

  - id: 9.11
    title: Scheduled hard-purge Lambda
    agent: backenddeveloper
    done: true
    depends_on: [9.5]
    tracking_issue: 145
    acceptance_criteria:
      - infrastructure/src/functions/hard_purge/ Lambda runs daily via aws_cloudwatch_event_rule; executes DELETE FROM users WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days' against Aurora
      - Uses the existing db layer at infrastructure/src/layers/db/ and the existing Secrets Manager credential pattern (no new secret machinery)
      - Supports a per-user invocation mode (user_id input parameter) so story 9.12's purge_immediately branch can reuse this Lambda scoped to a single user_id. When invoked with a user_id, the WHERE clause is `WHERE user_id = :id AND deleted_at IS NOT NULL` — the 30-day age check is dropped (purge_immediately bypasses the window) but the soft-delete guard is RETAINED so the Lambda cannot hard-delete a user who hasn't been soft-deleted first. If the row is not soft-deleted, the Lambda returns rows_affected=0 and exits cleanly (no error)
      - Aurora ON DELETE CASCADE is verified to fire on siblings, friendships, friend_requests, bookmarks, and blocks. Verification is concrete, not assumed: the integration test below inserts one row in EACH cascading table referencing the test user pre-soft-delete and asserts zero rows for that user_id in each table post-purge
      - Integration test: insert a user, insert one row referencing that user in EACH of (siblings, friendships, friend_requests, bookmarks, blocks), soft-delete the user with deleted_at set to 31 days ago, invoke the Lambda (scheduled mode), assert the users row is gone AND each of the 5 cascading-table rows is gone; insert another user with deleted_at 29 days ago and assert that user remains
      - Per-user invocation test: invoke with explicit user_id input on a soft-deleted user whose deleted_at is only 1 day ago; assert the row is purged (bypassing the 30-day window)
    notes: ""

  - id: 9.12
    title: purge_immediately branch — HardDeleteUserChatMessages Lambda + workflow wiring
    agent: backenddeveloper
    done: false
    depends_on: [9.1, 9.4, 9.5, 9.6, 9.11]
    tracking_issue: 146
    acceptance_criteria:
      - infrastructure/src/functions/hard_delete_user_chat_messages/ Lambda receives `{user_id, room_ids: [...]}` as input (room_ids supplied by the state-machine Parameters block from 9.4's output — see 9.1). For each room_id, Query ChatMessages by room_id with a FilterExpression on sender_id, and DeleteItem each matching row. Same continuation-token contract as 9.6 (Choice → Task → Choice loop with `has_more` flag)
      - The Lambda does NOT Query ChatRoomMembership itself — by the time it runs, 9.4 has already deleted those rows; room_ids comes from input
      - Membership rows for the deleted user are already gone (deleted by 9.4 in the same flow); this Lambda does NOT re-delete them
      - DDB-only Lambda; no db layer required. Idempotent regardless of whether the Aurora users row still exists when invoked (HardPurgeNow may have run in a sibling parallel arm and removed it)
      - State machine wiring (in 9.1): the top-level Choice on purge_immediately=true routes through the purge-immediately branch in which the parallel cleanup block contains {SoftDeleteAurora chained to HardPurgeNow (Lambda from 9.11 invoked with user_id input), DeleteDynamoDBPersonalData, HardDeleteUserChatMessages (this Lambda)}. AnonymizeChatMessages is NOT in this branch — it is replaced, not followed, by HardDeleteUserChatMessages
      - The ChatRooms rows themselves remain deactivated (not deleted) — the surviving participant still sees the room in their history with the requester's messages simply missing (same shape as "the other person deleted all their messages and left the chat")
      - Integration test: trigger a deletion with purge_immediately=true on a user who has 3 messages across 2 rooms. Assert: users row is gone (not just soft-deleted); ChatMessages rows where sender_id matched the deleted user are GONE (not anonymized — Query the rooms and assert zero remaining messages from that sender_id); deleted user's ChatRoomMembership rows are gone; surviving participant's membership rows are present; ChatRooms rows are status='deactivated' (not deleted); audit row has dynamodb_retention='hard_deleted'
    notes: ""

  - id: 9.13
    title: End-to-end deletion test
    agent: backenddeveloper
    done: false
    depends_on: [9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12]
    tracking_issue: 147
    acceptance_criteria:
      - tests/integration/deletion_e2e_test.py signs up two users (A = deleter, B = survivor), has them become friends, exchanges three chat messages, registers a push token for A
      - A calls DELETE /v1/profile/me (purge_immediately=false), the test polls /v1/profile/me/deletion-status with A's JWT until SUCCEEDED
      - Soft-delete assertions: Cognito user A is deleted (AdminGetUser returns UserNotFoundException); Aurora users row for A has deleted_at set with PII nulled; all 3 ChatMessages rows A sent now have sender_id='[deleted-user]' with content preserved; ChatRooms row is status='deactivated' with reason='user_deleted_account'; A's ChatRoomMembership rows are empty (Query PK=A.user_id returns zero rows); B's ChatRoomMembership row for the shared room is still present; Notifications and PushNotificationTokens for A are empty; an audit row "deletion_completed" exists with dynamodb_retention='permanent_anonymized'
      - block_filter regression scenario (oq-9.C): in a parallel sub-test, B blocks A before A's deletion. After A is hard-purged via 9.11 (test invokes 9.11 with explicit user_id to bypass the 30-day wait), assert B's deck/list/feed queries return zero rows attributable to A, AND B's message-history Query on the (now-deactivated) shared room still returns the anonymized messages with sender_id='[deleted-user]'
      - purge_immediately variant: in a separate sub-test, sign up users C and D, exchange messages, then C calls DELETE with purge_immediately=true. Assert C's ChatMessages rows are GONE (Query the shared room returns only D's messages); audit row has dynamodb_retention='hard_deleted'
    notes: ""

# ---------------------------------------------------------------------------
# Resolved open questions — resolved 2026-06-20 via phase-9 brainstorm
# (phasebrainstorms/phase-9-account-deletion-brainstorm.md). Each resolution
# has been folded into the relevant story's acceptance_criteria above.
# Kept here as an audit trail of the decisions made.
# ---------------------------------------------------------------------------
resolved_questions:
  - id: oq-9.A
    title: ChatRoomMembership rows are never touched by any deletion story
    resolution: |
      RESOLVED — folded into story 9.4. The deleted user's ChatRoomMembership
      rows are BatchWriteItem-deleted after the ChatRooms UpdateItem; the
      surviving participant's membership row is preserved (so they can still
      see the deactivated room with anonymized history). Asserted in 9.4 and
      9.13 integration tests.

  - id: oq-9.B
    title: 30-day hard purge does not cascade to DynamoDB
    resolution: |
      RESOLVED — retain anonymized DDB history permanently in the default
      soft-delete path (the surviving participant's chat history is preserved).
      Documented at the row level via the `dynamodb_retention` field on the
      "deletion_completed" audit record (story 9.8). The purge_immediately
      path is the regulated-request escape hatch and hard-deletes DDB rows
      (story 9.12). No 9.11 scope change.

  - id: oq-9.C
    title: block_filter behavior after a blocked user's hard purge
    resolution: |
      RESOLVED — safe-by-construction. After hard-purge, A's `users` row is
      gone, so every deck/list/feed query (all of which join to `users`)
      cannot surface A. The anonymized ChatMessages with sender_id='[deleted-user]'
      live only inside the deactivated shared room and are correctly rendered
      as anonymized history there. No schema change. Regression test added to
      story 9.13.

  - id: oq-9.D
    title: purge_immediately path needs DDB-side cleanup too
    resolution: |
      RESOLVED — in the purge_immediately branch, AnonymizeChatMessages is
      REPLACED (not followed) by a new HardDeleteUserChatMessages Lambda
      (story 9.12) that issues DeleteItem on each ChatMessages row where
      sender_id matches AND BatchWriteItem-deletes the deleted user's
      ChatRoomMembership rows. The ChatRooms rows remain deactivated so the
      surviving participant's room history is preserved with the deleter's
      messages simply gone. The state machine forks on purge_immediately at
      the top level (story 9.1).
