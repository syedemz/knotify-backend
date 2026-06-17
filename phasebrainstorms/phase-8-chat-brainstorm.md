# Phase 8 brainstorm — Chat (AppSync + DynamoDB Streams + push fan-out)

## 2026-06-17 brainstorm

Findings against `implementationplan/phase-8-chat.md`, cross-checked against `architecture.md §5.4 / §5.4.1 / §5.4.2 / §8.2`, the phase-2 DynamoDB module (already provisions all six chat tables incl. streams on `ChatMessages` + `Notifications`), phase-6 blocks Lambda, phase-7 hotfix lessons (#86 source_arn, #106 VPC endpoints, #109 JWT claim propagation), and the v1 §5.4 schema.

### Critical gaps (would break the phase if not addressed)

1. **Schema/subscription drift: stories 8.7 and 8.9 publish to subscriptions that story 8.2 does not declare.**
   - Story 8.7 references an `onReadReceipt` subscription; story 8.2's AC enumerates ONLY `onMessageInRoom, onNotificationForMe, onTypingInRoom, onRoomDeactivated, onFriendRequestUpdated` and asserts "schema contains no auto-generated subscriptions." `onReadReceipt` is missing from 8.2's declared schema → 8.7 will fail.
   - Story 8.9's AC says "onRoomReactivated fires, sendMessage succeeds again." `onRoomReactivated` is also not in 8.2's enumerated list. Architecture §5.4.1 explicitly calls for `roomReactivated` on unblock — the design intent is correct, the PRD's schema list is wrong.
   - Fix: extend story 8.2's enumerated subscription list to include `onReadReceipt(roomId)` and `onRoomReactivated(roomId)`, each `@aws_subscribe`-bound to the corresponding mutation (`markAsRead` / a server-only `_publishRoomReactivated` mutation — see finding #2).

2. **No publishing mutation for `onRoomDeactivated` / `onRoomReactivated`.** AppSync's `@aws_subscribe` requires a named mutation to fire the event. Block/unblock happens in `knotify-blocks` (REST), not in an AppSync mutation. Three viable mechanisms — pick one and document in 8.9:
   - (a) Blocks Lambda invokes a SigV4-signed AppSync mutation (e.g., `_publishRoomDeactivated(roomId, payload)`) annotated `@aws_iam`. Requires IAM-mode auth on AppSync (already AC of 8.1) and a backend-only directive on the mutation so JWT clients cannot call it.
   - (b) ChatRooms DynamoDB Stream → small Lambda → AppSync mutation. Adds a hop but keeps the blocks Lambda DynamoDB-only.
   - (c) Forward the stream record to a Lambda that hits AppSync HTTP endpoint via SigV4. Same shape as (a) but driven by stream, not by blocks.
   Default recommendation: **(a)** — least latency, blocks Lambda already touches DynamoDB so adding an AppSync SigV4 call is a small marginal cost. Whatever path is chosen, 8.9's AC must enumerate it concretely.

3. **No story for the chat resolver Lambda scaffold + IAM role + AppSync Lambda data source.**
   Stories 8.3 / 8.4 / 8.5 / 8.7 / 8.8 all reference "Lambda resolver" or "Lambda data source" but no story creates the Lambda function, the Terraform module shell, or the IAM role granting DynamoDB R/W on the five chat tables + Aurora `app_user` secret access + VPC config (resolvers must call Aurora for friendship/block checks per §5.4.1). Same issue as phase-7 brainstorm finding #5 — was resolved there by splitting story 7.0. Suggest:
   - **8.0** (new): `knotify-chat-resolver` Lambda scaffold (empty dispatcher, `__init__.py`, requirements, 0-route unit tests), `chat_resolver` IAM role (VPC + Aurora secret + DynamoDB IAM on all five chat tables), Terraform module `infrastructure/modules/chat_resolver`, wired in dev+prod `main.tf` with ARM64 + `knotify_db` + `knotify_obs` layers.
   - **8.0a** (new): AppSync Lambda data source `chat_resolver_ds` declared in the appsync module (consumed by 8.3 / 8.4 / 8.5 / 8.7 / 8.8). Story 8.1's third AC mentions "one Lambda data source for cross-Aurora calls" but this Lambda is the resolver Lambda itself, not a separate one — clarify.
   The current 8.1's "data sources are declared" wording lets a subagent infer "I'll do it inline" but the actual Lambda + IAM is much heavier than that line allows.

4. **PushFanout VPC egress: hotfix #106 trap applies here.**
   Story 8.10 calls `https://exp.host/--/api/v2/push/send`. If the fan-out Lambda is placed in private subnets without NAT egress (the current state per hotfix #106 — only Interface VPC endpoints for `secretsmanager`, `cognito-idp`, `lambda` exist), the call SYN-blackholes for 30 s exactly as the profile PATCH did. There is no Interface VPC endpoint for `exp.host`. Three options:
   - (a) Run PushFanout **outside** the VPC. Cheapest, requires no Aurora access (correct — fan-out reads DynamoDB only, and DynamoDB has no VPC requirement). **Default recommendation.**
   - (b) Provision NAT gateway. Expensive (≈$32/mo dev + $32/mo prod) for a single egress consumer.
   - (c) Use SNS → APNS/FCM directly (no third-party HTTP call). Larger refactor, decided against in architecture §13 #7.
   Story 8.10 must specify "Lambda runs outside the VPC" or this phase will repeat hotfix #106's diagnostic loop.

5. **Story 8.9 mutates a phase-6 file but is owned by phase 8 — must enumerate the edits.**
   "The knotify-blocks Lambda from phase 6 is extended so a block also performs a DynamoDB UpdateItem" requires: editing `infrastructure/src/functions/blocks/handler.py`, adding `dynamodb:UpdateItem` on `ChatRooms` to the blocks Lambda IAM role (currently has Aurora-only access via `aurora_writer` role), repackaging `blocks.zip`, terraform applying. The PRD does not say any of this. A subagent that reads "extended so a block also performs a DynamoDB UpdateItem" can produce code without the IAM update; the change then 500s in dev because of `AccessDeniedException`. AC must enumerate: file edited, IAM policy modified, redeploy required.

### Significant gaps

6. **Carryover from phase 6 not threaded into 8.3 / 8.4.**
   The PRD's carryover block ("chat-room-without-backing-friendship is read-only") is recorded but no story implements it. Story 8.4's `sendMessage` AC must add a check: after `GetItem(ChatRoomMembership)` and `GetItem(ChatRooms)`, query Aurora `friendships` (canonical pair) — if no row, reject with `RoomReadOnly`. The check belongs in `sendMessage` (per-message overhead acceptable; the `friendships` table is indexed on canonical pair). Alternative: cache `friendship_active` boolean on `ChatRoomMembership` and refresh on friend/unfriend events — more efficient, more state, defer to v2.
   Decision needed; the carryover MUST land in either 8.3 (room creation already checks this — covered) or 8.4 (sendMessage path), and 8.3 already checks "are caller and other user friends" — the gap is the **post-creation** drift case (friends → message sent → user defriends → other user tries to send message): with current 8.3 logic this would succeed.

7. **Story 8.4 missing `ClientRequestToken` for `TransactWriteItems` idempotency.**
   `TransactWriteItems` is idempotent ONLY when `ClientRequestToken` is provided (10-minute window). Without it, a Lambda retry (resolver timeout → AppSync retry) inserts the message twice with different SKs. AC must specify: ClientRequestToken = the client-supplied request idempotency key OR a deterministic value derived from `(sender_id, room_id, content_hash, second-precision-timestamp)`.

8. **Story 8.11 POST /v1/push-tokens — route wiring not enumerated; hotfix #86 trap.**
   "Route registered on HTTP API with Cognito JWT authorizer" is one line. Per the phase-6 pattern (each Lambda's route wiring was its own story with explicit `source_arn = module.api_gateway.api_execution_arn`, never `default_stage_arn`), the AC must explicitly state:
   - `aws_apigatewayv2_integration.push_tokens`
   - `aws_apigatewayv2_route.post_push_tokens` (POST /v1/push-tokens) with `authorization_type=JWT` + `authorizer_id=module.api_gateway.authorizer_id`
   - `aws_lambda_permission.push_tokens_api_gateway` with `source_arn = module.api_gateway.api_execution_arn`
   - `test_route_wiring.py` `_EXPECTED_ROUTE_KEYS` extended from 19 to 20.
   Without this, the subagent may repeat hotfix #86 (5xx, no Lambda invocation).

9. **`@require_profile_complete` gate for chat: defer or apply?**
   Phase-7 story 7.0b added the decorator that fail-closes any caller whose JWT lacks `custom:profile_complete = "true"`. Three relevant questions for phase 8:
   - Does `createOrGetRoom` (AppSync, not REST) require it? Architecture §5.4 implies yes — chat is post-onboarding.
   - Does `POST /v1/push-tokens` (story 8.11) require it? Probably no — tokens register during app first-launch, often before completion. Decide explicitly.
   - The decorator currently keys off REST handler signatures. AppSync resolvers receive a different event shape (`event["identity"]["claims"]["custom:profile_complete"]`). Either build a parallel decorator for resolvers, or do an inline check in the chat resolver dispatch. State which.

10. **AppSync `user_pool_config` not in story 8.1.**
    `aws_appsync_graphql_api.authentication_type = "AMAZON_COGNITO_USER_POOLS"` requires a `user_pool_config { user_pool_id, aws_region, default_action }` block. Story 8.1 says "referencing the Cognito User Pool from phase 4" but does not name the block. Add to AC: `user_pool_config.default_action = "DENY"` (no implicit fall-through), `user_pool_id = module.cognito.user_pool_id`, `aws_region = data.aws_region.current.name`.

11. **`additional_authentication_provider AWS_IAM` consumer not named.**
    Story 8.1 declares IAM as secondary auth "for backend" but no story names a consumer. The publishing mutations from finding #2 are the consumer; the PRD should connect the dots. Add to 8.1's notes: "IAM secondary mode supports backend Lambdas invoking publishing mutations (`_publishRoomDeactivated`, `_publishRoomReactivated`, `_publishReadReceipt`) via SigV4 — those mutations are annotated `@aws_iam` so JWT clients cannot invoke them."

12. **Story 8.13 e2e depends on 8.11 but does not exercise it.**
    `depends_on: [8.4, 8.6, 8.7, 8.9, 8.10, 8.11]` — 8.11 is push-tokens registration. The e2e narrative does NOT POST tokens. Either:
    - Add an explicit AC to 8.13: "for each test user, POST /v1/push-tokens with a fixture token before sendMessage; assert the Expo Push mock receives a call with that token."
    - Drop 8.11 from 8.13's depends_on.
    The current AC ("Expo Push mock receives the corresponding push call within 5 seconds") is unverifiable without a registered token — so the implied option is "add the POST step."

### depends_on errors

13. **Story 8.6 missing `depends_on` on 8.4.** AC: "A and B in room R, A sends a message → B's onMessageInRoom subscription receives the Message" — this requires `sendMessage` (8.4) to exist. Current `depends_on: [8.3]` is insufficient. Add 8.4.

14. **Story 8.9 missing `depends_on` on 8.4.** AC: "sendMessage in R now returns RoomDeactivated. A unblocks B → sendMessage succeeds again" — requires 8.4. Current `depends_on: [8.3]` is insufficient. Add 8.4.

### Non-testable acceptance criteria

15. **Story 8.8 "verified by examining a CloudTrail trace of the mutation call."**
    AppSync mutations on a `None` data source do not appear in CloudTrail (CloudTrail captures the AWS API call, not the GraphQL resolution). DynamoDB writes would appear in CloudTrail data events (off by default, expensive on). Rephrase: "verified by asserting no `dynamodb:PutItem` / `dynamodb:UpdateItem` call appears in a `boto3` botocore stub during the integration test" — or simply "unit test asserts the resolver function does not invoke any DynamoDB client method."

16. **Story 8.5 "BatchGet" but doesn't say which keys.**
    "fetched in a parallel BatchGet" — BatchGetItem on `ChatRooms` keyed by the `room_id` values returned from the membership Query. Spell this out: "BatchGetItem(ChatRooms, Keys=[{room_id: r1}, …])" — and note BatchGetItem's 100-item cap (for v1, a single user with >100 rooms is unrealistic; flag for phase-11 if it becomes a concern).

### Drift since plan was written

17. **DynamoDB tables already provisioned (phase 2 / story 2.x).**
    All six tables exist with streams on `ChatMessages` (`NEW_IMAGE`) and `Notifications` (`NEW_IMAGE`), PITR, deletion_protection. Story 8.1's third AC ("data sources are declared for the DynamoDB tables") implies the module references the existing table ARNs via `module.dynamodb.chat_rooms_arn` etc. Verify that the dynamodb module's `outputs.tf` exports the ARN + stream ARN for each table; if it doesn't, story 8.0 (or 8.1) must add them. Quick check: `infrastructure/modules/dynamodb/outputs.tf` already exposes `chat_rooms_arn`, `chat_messages_arn`, `chat_messages_stream_arn`, `notifications_arn`, `notifications_stream_arn`, `chat_room_membership_arn`, `message_reads_arn`, `push_notification_tokens_arn` (per grep). Confirmed — story 8.1 can reference them directly.

18. **Phase-7 hotfix #109 JWT claim flow applies to chat.**
    `cognito_pre_token_generation` emits `custom:user_sex` and `custom:profile_complete`. Chat resolvers reading `event.identity.claims` get these for free. Mention in story 8.0 / 8.3 notes: if any chat Aurora query requires `app.requesting_user_sex` GUC (unlikely — friendship and block lookups don't filter by sex), the claim is available; otherwise just rely on `sub`.

19. **Phase 0 / Makefile pattern.**
    Each new Lambda added to `package-all` in `Makefile`. Story 8.0 (new, see finding #3) and 8.10 (push_fanout) and 8.11 (push_tokens) and 8.12 (stale_token_cleanup) each add a new zip. Mention in their notes so the subagent doesn't forget.

### External dependencies / assumptions

20. **Expo Push access token: unauthenticated mode acceptable?**
    Expo Push HTTP API supports unauthenticated POST in EAS legacy mode but requires an `expo-access-token` for new projects. Story 8.10 hardcodes the URL but doesn't declare the auth strategy. Decide:
    - (a) Unauthenticated — simple, rate-limited per IP, fine for dev.
    - (b) Generate an Expo Access Token, store in `knotify-<env>-expo-push-credential` Secrets Manager secret, fan-out Lambda reads on cold start.
    For v1 dev: (a). For prod: (b). Add to 8.10's notes.

21. **`@aws_subscribe` filter expressions for `onMessageInRoom`.**
    AppSync `@aws_subscribe` filters by argument equality on the mutation result. `onMessageInRoom(roomId: ID!)` subscribed by client → only receives Message rows where `mutation.message.roomId == subscription.roomId`. Confirm the `sendMessage` mutation result type includes `roomId` (it does in §5.4). Story 8.2 AC should explicitly say each subscription's `@aws_subscribe` carries a `field` filter matching the mutation argument.

22. **AppSync field-level CloudWatch logs require a separate IAM role.**
    Story 8.1's AC: "log_config writes to CloudWatch at FIELD level (cloudwatch_logs_role_arn provided)." That role must trust `appsync.amazonaws.com` and carry `AWSAppSyncPushToCloudWatchLogs` managed policy. Add to `iam_roles` module; surface in 8.1's AC list.

### Lessons we should not relearn

23. **Hotfix #86 (source_arn).** Restated in finding #8.
24. **Hotfix #106 (VPC egress).** Restated in finding #4.
25. **Hotfix #87 (block_filter alias-qualified columns).** Chat resolvers querying Aurora for blocks must use `knotify_db.block_filter("u.user_id")` not roll their own. Spell out in story 8.0's IAM-and-deps brief.

---

### Summary

Five critical gaps (#1–#5), seven significant gaps (#6–#12), two `depends_on` errors (#13–#14), two non-testable ACs (#15–#16), three drift items (#17–#19), three external assumptions (#20–#22), three lesson reinforcements (#23–#25). The schema/mutation/subscription contract in story 8.2 is the most consequential — if 8.2 ships with the wrong list, stories 8.7 and 8.9 cannot pass their own tests. Recommend addressing #1, #2, #3, #4, #5, #6 in the PRD before proceeding; the rest can be folded into individual story notes during dispatch.

---

## 2026-06-17 16:30 brainstorm (re-run after PRD updates)

Re-review against the updated PRD. The 25 prior findings have all been addressed
(verified by re-reading the story ACs). A fresh pass surfaces three NEW gaps,
introduced by the additions themselves or uncovered by reading more of the
existing codebase.

### New critical gaps

A. **Story 8.9b only handles friend-request ACCEPT — but phase-6 friends Lambda
   also exposes `DELETE /v1/friends/{userId}` (unfriend).** Confirmed by reading
   `infrastructure/src/functions/friends/handler.py:271 (_handle_delete_friend)`.
   When user A unfriends user B, the friendship row is deleted but no story
   flips `ChatRooms.friendship_active=false`. Consequence: A and B are friends,
   chat, A unfriends B (no block) — the chat room still allows sendMessage
   because the cached `friendship_active` flag is stale at `true`. Defeats the
   whole point of the cache.

   Fix: extend story 8.9b's AC list with a fourth deliverable:
   - In `_handle_delete_friend`, AFTER the Aurora DELETE FROM friendships,
     perform a conditional UpdateItem on ChatRooms setting friendship_active=false.
     (Same IAM permission already granted in step 2; same redeploy story.)
   - Add integration test: A and B are friends and chatted, A calls DELETE
     /v1/friends/B → friendship_active flips to false; next sendMessage by
     either party returns RoomReadOnly.

B. **`publishNotification` and `_publishFriendRequestUpdated` mutations are
   declared in story 8.2 but no story implements their invocation.** Story
   8.9a's room-state publisher only handles `_publishRoomDeactivated` /
   `_publishRoomReactivated`. Result: the `onNotificationForMe` and
   `onFriendRequestUpdated` subscriptions never fire — the in-app real-time
   path for friend requests, profile views, bookmarks, etc. is broken.

   Architecture §5.4.2 claims "AppSync's subscription filters automatically
   detect the matching row" — that is wrong for our hand-written schema (we
   explicitly rejected Amplify auto-subscriptions, which were the only way
   that claim was true). With `@aws_subscribe(mutations: [...])` bindings,
   the subscription only fires when the named mutation runs.

   Fix options (pick one):
   - (i) Extend story 8.9a's stream-publisher Lambda to ALSO subscribe to the
     Notifications table stream (which already exists per phase 2) and
     invoke `publishNotification` on INSERT, and to the friend_requests
     row-write path to invoke `_publishFriendRequestUpdated`. The latter is
     harder because friend_requests is Aurora, not DynamoDB — no stream.
   - (ii) Add a new story 8.9c: a Notifications-table-stream publisher Lambda
     that calls `publishNotification(payload)` on every Notifications row
     INSERT. Friend-request changes would need the phase-6 friends Lambda
     extended to invoke `_publishFriendRequestUpdated` directly (since
     friend_requests is in Aurora). Same DDB-or-direct pattern as 8.9a / 8.9b.
   - (iii) Defer onNotificationForMe and onFriendRequestUpdated to a later
     phase. The in-app real-time toast for non-chat events doesn't ship until
     then. Push notifications still work via 8.10 (channel B).

   RECOMMENDED: (ii) — keeps the pattern consistent. Small new story.

### Significant gaps

C. **Story 8.4's "monkey-patch `time.time` in integration test" is mechanically
   incoherent for a deployed Lambda.** You cannot monkey-patch the clock of a
   running Lambda from the test runner. Two ways to resolve:
   - Re-classify the idempotency-replay test as a UNIT test (calls the
     handler function directly with a stubbed `time.time`). Keep the
     integration test for the success-path only. Cleaner.
   - Add a debug-only `clientRequestToken` override header that the resolver
     uses if present and the event is sourced from an "integration test"
     authorizer claim. Ugly, leaks test concerns into prod code. AVOID.

   Recommend the first option: move the idempotency replay assertion to the
   unit-test AC bullet that already exists ("Unit test: the token derivation
   function is pure..."), and drop the integration-test idempotency bullet.

### Drift since update

D. **Story 8.9a says "stream_enabled was previously off" for ChatRooms — confirmed**
   against `infrastructure/modules/dynamodb/main.tf:28-53`. The story's
   plan to enable `NEW_AND_OLD_IMAGES` on ChatRooms is correct. Adding a
   stream to an existing DynamoDB table does NOT require table replacement —
   in-place change. No data loss risk. Heads-up only.

E. **Stream-filter optimization deferred.** Story 8.9a's publisher Lambda
   receives every MODIFY on ChatRooms — including every `last_message_at`
   write that happens on every sendMessage. The Lambda no-ops correctly
   when status didn't change, but the invocation cost is real. At chat volume
   this is non-trivial. Phase-11 hardening concern, not blocking.
   Add a `notes:` line on 8.9a flagging it for the phase-11 PRD.

### Summary

Two critical (A: unfriend path missing, B: non-room publishers missing), one
significant (C: clock-pinning mechanism), one drift (D: confirmed safe),
one deferral note (E: stream filter optimization for phase 11).

A and B both reflect the same lesson: subscription/mutation bindings declared
in story 8.2 imply implementation stories for both sides of the contract.
Easy to miss when stories are written before the schema is fully nailed down.

## 2026-06-17 17:00 brainstorm (third pass — after 8.9b/8.9c/8.4 patches)

Findings against the patched `implementationplan/phase-8-chat.md`. Cross-checked
against `infrastructure/modules/dynamodb/main.tf` (verified Notifications stream
state) and the AppSync subscription filter semantics in story 8.6.

### Minor gaps

**F. Story 8.9c's stream-enablement wording is stale — Notifications already streams.**
   - The current AC says "stream_enabled with NEW_IMAGE — enabled by this story
     in modules/dynamodb/main.tf if not already on; check phase-2 brainstorm
     finding #17 again before flipping."
   - Verified: `infrastructure/modules/dynamodb/main.tf:253-254` already has
     `stream_enabled = true` + `stream_view_type = "NEW_IMAGE"` on the
     Notifications table (phase 2 provisioned it for PushFanout consumption).
   - Fix: drop the "enable if not already on" caveat. Story 8.9c just consumes
     the existing stream; no DDB module change is in scope. Tighten the AC to
     "consumes the existing Notifications DynamoDB Stream (stream_enabled was
     turned on in phase 2 with NEW_IMAGE)."

**G. Two ESMs on the same Notifications stream (8.9c + 8.10) — at the AWS default consumer limit.**
   - AWS DynamoDB Streams default limit: **2 simultaneous consumers** per stream
     for Lambda event source mappings. Story 8.10's PushFanout already maps the
     Notifications stream; story 8.9c adds a second mapping.
   - 2 consumers = exactly at the limit, not over it. No action required, but
     worth a one-line note in 8.9c's `notes:` so a future third consumer
     (observability stream-to-S3? account-deletion audit?) doesn't silently fail
     the EventSourceMapping create.
   - Fix: append to 8.9c notes — "Notifications stream now has 2 ESM consumers
     (this Lambda + push_fanout from 8.10) which is at the AWS default limit.
     A third consumer would require switching to Kinesis Data Streams for
     DynamoDB or a fan-out Lambda."

**L. Story 8.9c's `_publishFriendRequestUpdated` payload must carry the recipient `user_id` for identity-scoped subscription filtering.**
   - Story 8.6's AC for identity-scoped subscriptions: "onNotificationForMe is
     identity-scoped: pipeline first function filters on `user_id == identity.sub`."
     Same pattern applies to `onFriendRequestUpdated` (the schema in 8.2 says
     "filter by identity.sub").
   - For the filter to work, the AppSync mutation payload must contain a
     `user_id` field equal to the intended recipient's Cognito sub. The
     Notifications row already has `user_id` as PK — so the publisher just
     forwards it. But 8.9c's AC currently says
     `_publishFriendRequestUpdated(payload)` without specifying that `payload`
     must include `user_id`.
   - Fix: tighten 8.9c AC to spell out that the publish-mutation payload
     forwarded from the Notifications row must include `user_id` (the
     recipient's sub), which the subscription pipeline filters on. Same applies
     to `publishNotification(notification)` — `notification.user_id` is what
     onNotificationForMe filters by.

### Summary

Three minor cleanup items, all in story 8.9c. None block dispatch — if these
land as-is the subagent will probably write the Lambda correctly anyway
(NEW_IMAGE stream record contains the row, so user_id forwards naturally) —
but tightening the AC removes the ambiguity that could let a marginal
implementation slip through review.

## 2026-06-17 17:30 brainstorm (fourth pass — after 8.9c tightening)

Final scan of the patched PRD. End-to-end coherence check.

### What was verified clean
- All 17 stories (8.0, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.9a, 8.9b, 8.9c, 8.10, 8.11, 8.12, 8.13) have `agent: backenddeveloper`, non-empty `acceptance_criteria`, and `depends_on` entries that resolve to siblings within phase 8.
- The friendship_active flag lifecycle is fully covered: 8.3 sets true at room-creation, 8.9 flips false on block, 8.9b flips false on unfriend and true on accept, 8.4 gates sendMessage on it.
- Both backend-published mutation sets have implementing publisher Lambdas: 8.9a (room state) and 8.9c (notifications).
- Notifications stream's 2-ESM consumer footprint (8.9c + 8.10) is documented at the AWS default limit.
- Hotfix carryovers (#86 source_arn, #87 block_filter, #106 VPC egress, #109 JWT claims) are explicitly referenced in the affected stories (8.0, 8.10, 8.11).

### One residual nit

**M (COSMETIC). Story 8.1's AC singles out story 8.9a as the only AWS_IAM consumer.**
   - Current wording: "additional_authentication_provider AWS_IAM (secondary,
     consumed by the backend room_state_publisher Lambda in story 8.9a;
     publish mutations declared in 8.2 are annotated `@aws_iam` so user-JWT
     clients cannot invoke them)".
   - With 8.9c added, there are now TWO IAM-mode consumers (room_state_publisher
     AND notifications_publisher). Doesn't break anything — the AppSync API
     accepts arbitrary numbers of IAM-mode callers — but the AC's phrasing is
     stale. Tighten to "consumed by the backend publisher Lambdas (8.9a
     room_state_publisher and 8.9c notifications_publisher)".
   - Cosmetic. Skip if you want to dispatch.

### Summary

PRD is dispatch-ready. Only one cosmetic mention in story 8.1 lags slightly
behind the 8.9c addition. Recommend proceeding.

## 2026-06-17 17:45 brainstorm (fifth pass — after 8.1 cosmetic fix)

Final scan. Story 8.1's IAM-mode-consumer note now references both publisher
Lambdas (8.9a + 8.9c). No new findings — diminishing returns reached.

### What was re-verified
- 8.1 AC now correctly enumerates 8.9a + 8.9c as the AWS_IAM consumers.
- No drift between cross-cutting decisions, carryovers, and the per-story AC.
- No story smuggles future-phase scope (account deletion, observability,
  S3 photos stay deferred).
- depends_on graph is acyclic and topologically sortable.

### Topological dispatch order (for Step 1 onward)

Eligible immediately (depends_on: []): **8.0**, **8.11**.

After 8.0: 8.1 → 8.2 → 8.3 → 8.4, 8.5, 8.9c (3-way fan-out from 8.2's schema)
After 8.3: 8.4, 8.5, 8.6 (8.6 also needs 8.4)
After 8.4: 8.6, 8.7, 8.10
After 8.7: 8.8 (via 8.6)
After 8.9: 8.9a, 8.9b
After 8.9b: feeds 8.13
After 8.11: feeds 8.13 (independent of chat resolver chain)
After 8.12 (needs 8.11): independent
8.13 last (needs the world).

### Summary

PRD reached steady state. Proceed.

## 2026-06-17 18:30 brainstorm (sixth pass — pre-dispatch sweep)

Single new finding surfaced by spot-checking infrastructure outputs the prior
brainstorms assumed existed. Prior third-pass finding #17 claimed
`module.dynamodb.chat_rooms_arn` etc. were exported; re-checked against the
current `infrastructure/modules/dynamodb/outputs.tf` and that claim was wrong.

### New blocking gap

**N. Dynamodb module exposes only `*_table_name` and two `*_stream_arn` outputs — no table `_arn` outputs, no `chat_rooms_stream_arn`.**

   - Verified contents of `infrastructure/modules/dynamodb/outputs.tf`: outputs
     are `chat_rooms_table_name`, `chat_room_membership_table_name`,
     `chat_messages_table_name`, `message_reads_table_name`,
     `chat_messages_stream_arn`, `notifications_table_name`,
     `notifications_stream_arn`, `push_tokens_table_name`. That's it.
   - Multiple phase-8 stories scope IAM policies via "table ARNs sourced from
     module.dynamodb outputs":
       - 8.0 (chat_resolver role on all five chat tables)
       - 8.9 (blocks Lambda gets UpdateItem on ChatRooms)
       - 8.9a (room_state_publisher needs ChatRooms stream ARN — currently no
         output; the table's stream isn't even enabled yet, that's part of
         8.9a's scope, so the output must be added in the same story)
       - 8.9b (friends Lambda gets UpdateItem on ChatRooms)
       - 8.9c (notifications_publisher needs Notifications stream ARN —
         this one EXISTS as `notifications_stream_arn`; OK)
       - 8.10 (push_fanout: ChatMessages + Notifications stream ARNs both
         exist; but also needs table ARNs on ChatRooms, ChatRoomMembership,
         Notifications, PushNotificationTokens for GetItem/Query/UpdateItem/
         DeleteItem — none of those are exported)
       - 8.11 (push_tokens needs PushNotificationTokens table ARN — not
         exported)
       - 8.12 (stale_token_cleanup needs same — not exported)
   - The subagent dispatched on 8.0 will write a policy referencing
     `module.dynamodb.chat_rooms_arn` and `terraform validate` will fail
     with "Unsupported attribute: This object has no argument, nested block,
     or exported attribute named 'chat_rooms_arn'."
   - Fix scope: add the following outputs to
     `infrastructure/modules/dynamodb/outputs.tf` as part of story 8.0
     (it's the first story to need them, and a single small edit unblocks
     everything downstream):
       - `chat_rooms_arn` = `aws_dynamodb_table.chat_rooms.arn`
       - `chat_room_membership_arn` = `aws_dynamodb_table.chat_room_membership.arn`
       - `chat_messages_arn` = `aws_dynamodb_table.chat_messages.arn`
       - `message_reads_arn` = `aws_dynamodb_table.message_reads.arn`
       - `notifications_arn` = `aws_dynamodb_table.notifications.arn`
       - `push_notification_tokens_arn` = `aws_dynamodb_table.push_notification_tokens.arn`
     And as part of story 8.9a (which enables the ChatRooms stream):
       - `chat_rooms_stream_arn` = `aws_dynamodb_table.chat_rooms.stream_arn`
   - Recommendation: tighten story 8.0's AC list with one additional bullet
     — "Extend `infrastructure/modules/dynamodb/outputs.tf` with table-ARN
     outputs for all six chat tables (chat_rooms_arn,
     chat_room_membership_arn, chat_messages_arn, message_reads_arn,
     notifications_arn, push_notification_tokens_arn). The
     chat_rooms_stream_arn output is added later by story 8.9a alongside
     enabling the stream itself."

### Verified clean
- `knotify_obs.chat_room_id` helper already exists at
  `infrastructure/src/layers/observability/knotify_obs/_chat_room_id.py:15`
  and is exported via `__init__.py:41`. Story 8.3 can import directly.
- `knotify_obs.block_filter` and `knotify_obs.is_blocked` exist at
  `_blocks.py:57` and `_blocks.py:97`. Stories 8.0 / 8.3 / 8.4 can rely on
  them with no scaffolding.
- `@require_profile_complete_appsync` does NOT yet exist (only the REST
  variant `_profile_complete.py`). Story 8.0's AC already calls this out —
  correctly scoped.
- Phase-7 closed cleanly: `phase-7-complete` tag in place, four hotfixes
  merged (#106, #107, #108, #109), no in-flight branches or open PRs.
  `development` is clean.

### Summary

One blocking gap (N): the dynamodb module is missing the table-ARN outputs
that seven of seventeen stories assume. A single AC bullet on story 8.0
(plus the chat_rooms_stream_arn on 8.9a, which is already implicit there)
fixes it. Recommend addressing this one and proceeding — everything else
the prior five passes covered is still accurate.

## 2026-06-17 18:50 brainstorm (seventh pass — post-N fix)

### Verified fix
- Story 8.0 now has an explicit AC bullet enumerating the six table-ARN
  outputs to add to `infrastructure/modules/dynamodb/outputs.tf`
  (`chat_rooms_arn`, `chat_room_membership_arn`, `chat_messages_arn`,
  `message_reads_arn`, `notifications_arn`, `push_notification_tokens_arn`).
  Gap N closed.
- PRD `last_updated:` bumped to `2026-06-17 # sixth-pass brainstorm: 8.0 AC
  adds dynamodb module table-ARN outputs (gap N)`.

### No new findings

A fresh end-to-end scan after the edit surfaced nothing new. The 17 stories,
their depends_on graph, AC coverage of the friendship_active lifecycle, the
two publisher Lambdas (8.9a + 8.9c), the out-of-VPC placement of 8.10 and
8.12, and the hotfix-86/87/106/109 carryovers all remain consistent with
what the fifth-pass summary verified.

### Summary

PRD dispatch-ready. Proceed.
