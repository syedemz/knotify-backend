phase: 9
title: Account deletion (Step Functions, soft delete)
last_updated: 2026-06-19

context_summary: |
  Implements the account-deletion workflow per §11 of architecture.md with the §13 #8 resolution applied: soft delete (UPDATE users SET deleted_at, strip PII) with a 30-day retention before a scheduled hard purge via cascade. ChatMessages are anonymized rather than deleted per §13 #21 — sender_id rewritten to '[deleted-user]' while content is preserved. Step Functions Standard workflow orchestrates the steps; each step is an idempotent Python 3.14 Lambda. An audit log table records initiation and completion. A purge_immediately flag supports GDPR right-to-be-forgotten by skipping the 30-day wait. This phase ships after chat because the workflow needs to deactivate ChatRooms and anonymize ChatMessages — both DynamoDB tables created in phase 2 and operated on by phase 8.

stories:
  - id: 9.1
    title: Step Functions state machine module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/step_functions/main.tf declares aws_sfn_state_machine of type STANDARD with the definition expressed as a templated JSON file matching the §11.2 architecture state machine (ValidateDeletionRequest → DisableCognitoUser → DeactivateChatRooms → ParallelCleanup{SoftDeleteAurora, DeleteDynamoDBPersonalData, AnonymizeChatMessages} → DeleteCognitoUser → WriteAuditLog → NotifySuccess, with global Catch routing to DeletionFailed)
      - Each task references the matching Lambda ARN from the stories below
      - logging_configuration enabled at ALL with the 7-day retention CloudWatch log group
      - Module output: state_machine_arn
    notes: ""

  - id: 9.2
    title: ValidateDeletionRequest Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Lambda confirms the input user_id matches the JWT sub passed via input, checks the audit table for an in-progress deletion for the same user_id (idempotency), writes a "deletion_initiated" audit record
      - Integration test: a second invocation for the same user_id while a prior execution is still in-progress returns a "DeletionInProgress" error state for Step Functions
    notes: ""

  - id: 9.3
    title: DisableCognitoUser Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Lambda calls cognito-idp AdminDisableUser for the user_id; idempotent (catches UserNotFoundException and AlreadyDisabled-style cases and treats them as success)
      - Integration test: invoke on an existing Cognito user, then invoke again — both return success
    notes: ""

  - id: 9.4
    title: DeactivateChatRooms Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Lambda queries ChatRoomMembership PK=user_id for all room_ids, then UpdateItem on each ChatRooms row to set status='deactivated', deactivated_reason='user_deleted_account', deactivated_at=NOW(); publishes an AppSync mutation that fires onRoomDeactivated to the surviving participant
      - Conditional update skips rooms already deactivated for any reason (idempotent)
      - Integration test: user with 3 rooms — one already deactivated by block — invoke the Lambda; afterwards all 3 are deactivated; the one previously blocked keeps deactivated_reason='blocked' (most-recent-wins is NOT used; reason of first deactivation persists)
    notes: ""

  - id: 9.5
    title: SoftDeleteAurora Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Lambda runs the §11.1 step-2 SQL: UPDATE users SET deleted_at=NOW(), email=NULL, phone_number=NULL, photo_url=NULL, chosen_profile_avatar=NULL, preferences='{}', preference_vector=NULL, username='[deleted-user]', first_name='Deleted', last_name='User' WHERE user_id=:id AND deleted_at IS NULL
      - The UPDATE is conditional on deleted_at IS NULL so re-runs are no-ops
      - Integration test: invoke on a fresh user, assert the row has deleted_at set and PII fields nulled, AND the friendships rows still exist; invoke again, no change
    notes: ""

  - id: 9.6
    title: AnonymizeChatMessages Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Lambda paginates a Scan or GSI query (whichever is needed; an additional GSI on sender_id may be added to ChatMessages — if added, declared in the lambda module's terraform plan) over the user's ChatMessages and UpdateItem each row to set sender_id='[deleted-user]'
      - The handler accepts and returns a continuation token so Step Functions can iterate via a Map state until done
      - Integration test: seed a user with 25 messages across 3 rooms, invoke (single call), assert all 25 rows have sender_id='[deleted-user]' and content is unchanged
    notes: ""

  - id: 9.7
    title: DeleteDynamoDBPersonalData Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Lambda deletes all Notifications rows where user_id=:id (Query then BatchWriteItem deletes in batches of 25) and all PushNotificationTokens rows where user_id=:id
      - Idempotent: re-running on an empty result set returns success
      - Integration test: seed 30 notifications and 2 push tokens for a user, invoke, assert zero remain
    notes: ""

  - id: 9.8
    title: Audit log table and WriteAuditLog Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - A new DynamoDB table account_deletion_audit (PK user_id, SK event_id) is added to the dynamodb module with TTL on attribute "expire_at" set to created_at + 7 years
      - WriteAuditLog Lambda writes a "deletion_completed" record with branches_succeeded list, completed_at, execution_arn
      - Integration test: drive a full execution and assert both "deletion_initiated" and "deletion_completed" rows exist in the table
    notes: ""

  - id: 9.9
    title: knotify-deletion-initiator Lambda and DELETE /v1/profile/me route
    agent: backenddeveloper
    done: false
    depends_on: [9.1]
    acceptance_criteria:
      - src/functions/deletion_initiator/ Lambda extracts user_id from the JWT sub, calls states StartExecution on the state machine from 9.1 with input {user_id, purge_immediately: <body flag, default false>}, returns 202 with executionArn
      - Route DELETE /v1/profile/me wired on HTTP API with Cognito JWT authorizer
      - Integration test: signup a user, call DELETE /v1/profile/me, observe 202 response, poll DescribeExecution until SUCCEEDED, assert users row has deleted_at set
    notes: ""

  - id: 9.10
    title: GET /v1/profile/me/deletion-status endpoint
    agent: backenddeveloper
    done: false
    depends_on: [9.9]
    acceptance_criteria:
      - The knotify-deletion-initiator Lambda also handles GET /v1/profile/me/deletion-status?executionArn=<arn> by calling DescribeExecution and returning status, startDate, stopDate, and the names of completed steps
      - Returns 403 if the executionArn's input.user_id does not match the JWT sub (prevent cross-user status reads)
      - Integration test: poll the endpoint during a running deletion, observe state transitions
    notes: ""

  - id: 9.11
    title: Scheduled hard-purge Lambda
    agent: backenddeveloper
    done: false
    depends_on: [9.5]
    acceptance_criteria:
      - src/functions/hard_purge/ Lambda runs daily via aws_cloudwatch_event_rule; executes DELETE FROM users WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days' against Aurora
      - The cascade (siblings, friendships, friend_requests, bookmarks, blocks) is verified to fire
      - Integration test: insert a user, soft-delete it with deleted_at set to 31 days ago, invoke the Lambda, assert the users row and a previously-inserted siblings row are both gone; insert another user with deleted_at 29 days ago and assert it remains
    notes: ""

  - id: 9.12
    title: purge_immediately path through the workflow
    agent: backenddeveloper
    done: false
    depends_on: [9.1, 9.5, 9.11]
    acceptance_criteria:
      - When the Step Functions input includes purge_immediately=true, after SoftDeleteAurora completes the workflow routes to a new HardPurgeNow state (Lambda invocation of the phase 9.11 logic scoped to the specific user_id) before continuing to DeleteCognitoUser
      - Integration test: trigger a deletion with purge_immediately=true, observe the users row is gone (not just soft-deleted) by the end of the execution
    notes: ""

  - id: 9.13
    title: End-to-end deletion test
    agent: backenddeveloper
    done: false
    depends_on: [9.9, 9.6, 9.7, 9.4, 9.8]
    acceptance_criteria:
      - tests/integration/deletion_e2e_test.py signs up two users, has them become friends, exchanges three chat messages, registers a push token for the user who will delete
      - The user calls DELETE /v1/profile/me, the test polls deletion-status until SUCCEEDED
      - The test asserts: Cognito user deleted (AdminGetUser returns UserNotFoundException), Aurora users row has deleted_at set with PII nulled, all 3 ChatMessages rows the user sent now have sender_id='[deleted-user]' with content preserved, ChatRooms row is status='deactivated' with reason='user_deleted_account', Notifications and PushNotificationTokens for the user are empty, and an audit row "deletion_completed" exists
    notes: ""

# ---------------------------------------------------------------------------
# Open questions — surfaced 2026-06-19 from phase-8 E2E probe walkthrough.
#
# These are NOT acceptance criteria yet. They are gaps to resolve in the
# phase-9 brainstorm (Step 0 of /implement-phase 9). Each gap should either:
#   (a) be folded into an existing story's acceptance_criteria, OR
#   (b) spawn a new story with a clear decision, OR
#   (c) be explicitly accepted as "out of scope, document the rationale".
# ---------------------------------------------------------------------------
open_questions:
  - id: oq-9.A
    title: ChatRoomMembership rows are never touched by any deletion story
    affects: [9.4, 9.6]
    description: |
      Stories 9.4 (deactivate ChatRooms) and 9.6 (anonymize ChatMessages) touch
      the ChatRooms and ChatMessages tables, but no story removes the deleted
      user's ChatRoomMembership rows. The surviving participant's membership row
      also persists, and there is no cascade between Aurora's `users` delete and
      these DDB rows. Decisions needed:
        - Should the deleted user's ChatRoomMembership rows be removed (so
          listMyRooms stops returning the dead rooms server-side), or kept (so
          some hypothetical admin tool can still trace participation)?
        - Should the surviving participant's row stay (so they can still see
          message history under the [deleted-user] anonymized banner)?
      Leading answer: delete the deleted user's row, keep the surviving
      participant's row. Confirms with the "history preserved for survivors"
      principle from the phase-9 context summary.

  - id: oq-9.B
    title: 30-day hard purge does not cascade to DynamoDB
    affects: [9.11]
    description: |
      Story 9.11 lists the Aurora cascade (siblings, friendships, friend_requests,
      bookmarks, blocks) but does not touch DDB. After the 30-day window the
      ChatRooms rows still sit in `status=deactivated` indefinitely, the
      anonymized ChatMessages stay forever, and any surviving ChatRoomMembership
      rows reference a user_id that no longer exists in Aurora. Decisions needed:
        - Is permanent retention of anonymized chat history the intended policy
          (likely yes — preserves the other user's record)?
        - If yes, document the rationale explicitly in §11 of architecture.md
          and add a note in the audit log entry so legal/compliance has
          visibility.
        - If no, define a parallel DDB-side purge (separate Lambda? extend
          hard_purge to also issue DeleteItem on ChatRooms / ChatMessages
          where one of the participants is the user being purged?).
      Leading answer: retain anonymized DDB history permanently; document it.
      Right-to-be-forgotten (purge_immediately) is the escape hatch for
      regulated requests, and should also delete the DDB rows in that path.

  - id: oq-9.C
    title: block_filter behavior after a blocked user's hard purge
    affects: [9.5, 9.11]
    description: |
      If user A had been blocked by user B, and A's account is hard-purged
      (Aurora cascade removes B's `blocks` row referencing A), then any
      message-history query by B that previously called `block_filter()` to
      hide A would suddenly stop filtering — A's anonymized ChatMessages rows
      (sender_id='[deleted-user]') could resurface in B's UI. Decisions needed:
        - Does block_filter() correctly treat sender_id='[deleted-user]' as
          a non-joinable identity (so it neither blocks nor exposes, just
          renders as anonymized)? Almost certainly yes, but needs a test.
        - Should hard-purge skip cascade-delete on the `blocks` table to
          preserve B's "I never want to see anything from this person" intent
          across the dead-user transition?
      Leading answer: anonymized messages should remain blocked-equivalent by
      virtue of being anonymized. Add a regression test: B blocks A, A is
      hard-purged, B's message-history query for the (now-deactivated) shared
      room returns rows with sender_id='[deleted-user]' but B's deck/list/feed
      queries do NOT surface anything attributable to A.

  - id: oq-9.D
    title: purge_immediately path needs DDB-side cleanup too
    affects: [9.12]
    description: |
      Story 9.12 routes through HardPurgeNow (Aurora-only) for GDPR
      right-to-be-forgotten requests. But story 9.6 only anonymizes the
      ChatMessages — anonymized content of a user's chats remains in DDB
      forever. For a true right-to-be-forgotten, the messages the requesting
      user sent should be hard-deleted (DeleteItem) in this branch, not just
      anonymized. Decisions needed:
        - In the purge_immediately branch, should AnonymizeChatMessages be
          replaced (or followed) by a HardDeleteUserChatMessages step that
          issues DeleteItem on each ChatMessage where sender_id matches?
        - What happens to the other participant's UI when half a conversation
          vanishes? (Likely acceptable; the legal driver outranks UX continuity.)
      Leading answer: in the purge_immediately path, hard-delete the requesting
      user's ChatMessages and ChatRoomMembership rows. Keep the ChatRooms row
      (other participant's history of the room is preserved with the deleted
      user's messages gone — same shape as "the other person deleted all
      their messages and left the chat").
