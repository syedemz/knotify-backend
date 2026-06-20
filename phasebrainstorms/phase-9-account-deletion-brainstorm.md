# Phase 9 brainstorm — Account deletion (Step Functions, soft delete)

## 2026-06-19 16:30 brainstorm

Audit of `implementationplan/phase-9-account-deletion.md` against the live repo
state at `phase-8-complete`. Goal: surface acceptance-criteria gaps, scope
creep, wrong/missing `depends_on`, drift since planning, external assumptions,
and resolve the four explicit `open_questions` (oq-9.A through oq-9.D).

---

### A. Resolutions for the four PRD open_questions

These are the leading answers from the PRD itself, validated against the
current repo state. Each maps to a concrete PRD edit.

**oq-9.A — ChatRoomMembership rows are never touched**

- Live state: `infrastructure/modules/dynamodb/main.tf:80` declares
  `ChatRoomMembership` PK=user_id SK=room_id. Story 9.4 only writes to
  `ChatRooms`; story 9.7 only deletes `Notifications` + `PushNotificationTokens`.
  The deleted user's membership row survives indefinitely; the surviving
  participant's row also survives, which is correct.
- **Resolution:** delete the deleted user's `ChatRoomMembership` rows; keep the
  surviving participant's row. Soft-delete branch (9.4 or its parallel
  `DeleteDynamoDBPersonalData`) should issue `DeleteItem` on each
  `(user_id, room_id)` pair after the corresponding `ChatRooms` UpdateItem.
- **PRD edit:** fold into **story 9.4** (or split into 9.4b for separation of
  concerns — recommend folding to keep the state machine flat). Add an
  acceptance criterion: "After DeactivateChatRooms runs, the deleted user's
  ChatRoomMembership rows (PK=user_id) are removed; surviving participants'
  membership rows are untouched. Integration test asserts this."
- **Knock-on:** **story 9.13** must assert `ChatRoomMembership` for the deleted
  user is empty and the surviving user's row is present.

**oq-9.B — 30-day hard purge does not cascade to DynamoDB**

- Live state: confirmed Aurora cascade fires on `users` delete
  (`infrastructure/db/migrations/0003..0006` all use `ON DELETE CASCADE`).
  No DDB cleanup is wired into 9.11.
- **Resolution:** retain anonymized DDB history permanently; do not purge DDB
  in the 30-day path. Document the rationale in §11 of architecture.md and
  add a note on the audit-log row that DDB anonymization is permanent retention.
- **PRD edit:** add an acceptance criterion to **story 9.8** that the
  "deletion_completed" audit row includes a field
  `dynamodb_retention: "permanent_anonymized"` (or equivalent) so
  legal/compliance can see the policy at the row level. No 9.11 scope change.

**oq-9.C — block_filter behavior after a blocked user's hard purge**

- Live state: confirmed `infrastructure/db/migrations/0006_create_bookmarks_and_blocks.sql`
  declares `blocker_id ... ON DELETE CASCADE` AND
  `blocked_id ... ON DELETE CASCADE`. So when A is hard-purged, B's
  `blocks(blocker_id=B, blocked_id=A)` row is cascade-deleted.
- The safety property: A's user row is gone too, so A cannot appear in any
  deck/list/feed query (every such query joins to `users`). Anonymized
  ChatMessages rows surface only inside the (now-deactivated) shared room,
  and `block_filter()` is irrelevant there because `'[deleted-user]'` is not a
  valid UUID and never appears in `blocks`.
- **Resolution:** the property is safe-by-construction. Add a regression test
  rather than changing schema.
- **PRD edit:** add an acceptance criterion to **story 9.13** that simulates
  the chain: B blocks A → A is hard-purged via 9.11 → B's deck/list/feed
  queries return zero rows attributable to A → B's deactivated-room
  message history still renders anonymized messages.

**oq-9.D — purge_immediately path needs DDB-side cleanup too**

- Live state: 9.12 currently routes only through `HardPurgeNow` (Aurora-only).
  Anonymized ChatMessages and ChatRoomMembership would persist for a
  right-to-be-forgotten user.
- **Resolution:** in the `purge_immediately=true` branch, **replace** the
  parallel `AnonymizeChatMessages` step with a `HardDeleteUserChatMessages`
  step (DeleteItem on each row where sender_id matches), and **also**
  DeleteItem on the deleted user's `ChatRoomMembership` rows. Keep the
  `ChatRooms` row deactivated (other participant's history shape is "the
  other person deleted all their messages and left the chat").
- **PRD edit:** extend **story 9.12** acceptance criteria. The state machine
  branches at `ValidateDeletionRequest` (or just before `ParallelCleanup`) on
  `purge_immediately`: false → existing parallel branch; true → a parallel
  branch where the AnonymizeChatMessages task is swapped for a hard-delete
  task. Add an integration test that asserts ChatMessages rows are gone
  (not just anonymized) when `purge_immediately=true`.

---

### B. Acceptance-criteria gaps (testable / scope / completeness)

**B1. Story 9.1 — state machine references Lambdas that aren't stories.**
The architecture §11.2 state machine includes `DeleteCognitoUser` and
`NotifySuccess` (SNS) and a global `DeletionFailed` Catch. The PRD has 9.3
**DisableCognitoUser** but NO story for the **DeleteCognitoUser** terminal
step. Likewise no story creates the SNS topic for `NotifySuccess` or the
SNS topic for `DeletionFailed` alerting.
- **Recommend:** either add an explicit acceptance criterion to 9.3 that
  the Lambda exposes BOTH a "disable" mode and a "delete" mode (selected by
  input parameter), wired as two different tasks in the state machine; OR
  add a small story 9.3b for DeleteCognitoUser as a separate Lambda. Folding
  into 9.3 is simpler.
- For `NotifySuccess` / `DeletionFailed`: either drop them entirely (they
  are listed as optional in §11.2) or add a story 9.1b that creates an SNS
  topic and uses the Step Functions direct SNS service integration (no
  Lambda). Recommend **drop NotifySuccess** for v1 (no SES/email yet — §13
  item 20 is unresolved), keep `DeletionFailed` as a CloudWatch metric +
  alarm wired into the phase-10 observability work rather than a phase-9
  SNS topic.

**B2. Story 9.6 — Map state vs. continuation-token loop.**
"Step Functions can iterate via a Map state until done" — Step Functions
`Map` operates on arrays, not on continuation tokens. The correct shape is
a `Choice` → `Task` → `Choice` loop with `HasMore` in the output, OR an
in-Lambda paginated loop bounded by Lambda timeout. Given a 15-min Lambda
ceiling and chat-message volumes for a normal user, an in-Lambda loop is
fine; the continuation token is only needed for users with >100k messages.
- **Recommend:** rewrite the criterion as "in-Lambda paginated loop; if a
  continuation token survives the Lambda timeout, return it and Step
  Functions invokes the Lambda again via a Choice loop." Drop the "Map
  state" phrasing.

**B3. Story 9.6 — sender_id GSI declaration site.**
"an additional GSI on sender_id may be added to ChatMessages — if added,
declared in the **lambda module's terraform plan**". GSIs are declared on
the table itself (`infrastructure/modules/dynamodb/main.tf`), not on the
Lambda module.
- **Recommend:** pin the answer. Without a GSI, the only options are
  full-table `Scan` (unbounded cost as chat volume grows) or
  `Query` per room driven by ChatRoomMembership PK=user_id (only rooms
  the user was in — much cheaper). The membership-driven query is
  preferable. State this explicitly: "iterate the user's
  ChatRoomMembership rows to get room_ids, then Query ChatMessages by
  room_id and filter on sender_id." Cost: linear in messages-in-rooms-
  the-user-was-in, not linear in all chat traffic. **No new GSI needed.**

**B4. Story 9.7 — what about ChatRoomMembership?**
oq-9.A resolution adds membership-delete to 9.4 (DeactivateChatRooms). If we
instead put it in 9.7 (DeleteDynamoDBPersonalData), then 9.7 must also
query ChatRoomMembership for the user_id. Either home is defensible. The
PRD currently puts membership-related work in 9.4; keeping that grouping
matches the §11.1 #3 narrative (DDB chat-shape changes in one Lambda,
notifications/tokens in another).
- **Recommend:** add the membership-delete to **story 9.4**, not 9.7.

**B5. Story 9.8 — TTL attribute unit.**
"TTL on attribute 'expire_at' set to created_at + 7 years." DynamoDB TTL
requires Unix epoch **seconds** in a Number attribute. Spell that out so
the subagent doesn't accidentally write an ISO string.
- **Recommend:** "DynamoDB TTL configured with attribute `expire_at` (Number,
  Unix epoch seconds). Writer Lambda computes
  `expire_at = int(time.time()) + 7*365*86400` per row."

**B6. Story 9.9 — module path.**
"src/functions/deletion_initiator/" — the actual layout is
`infrastructure/src/functions/deletion_initiator/` (confirmed by `ls`). Same
typo in story 9.11.
- **Recommend:** path corrected to `infrastructure/src/functions/...` in
  both stories. Cosmetic, but eliminates a reviewer round-trip.

**B7. Story 9.9 — DELETE /v1/profile/me collision.**
The `profile` Lambda already owns `GET /v1/profile/me` and
`PUT /v1/profile/me` (verified in route wiring tests). Adding DELETE on the
same path is fine in HTTP API (route key is method-scoped), but the new
DELETE integration must point at the deletion_initiator Lambda, not the
profile Lambda. Spell out: "DELETE /v1/profile/me is wired as a separate
aws_apigatewayv2_integration to the deletion_initiator Lambda; the profile
Lambda is unaffected."

**B8. Story 9.10 — JWT-vs-execution-input verification cost.**
"Returns 403 if the executionArn's input.user_id does not match the JWT
sub." `DescribeExecution` returns the input as a JSON string and is a
single API call — acceptable. Make the criterion explicit: "calls
DescribeExecution, parses `execution.input` as JSON, asserts
`input.user_id == jwt.sub`; otherwise 403 with no execution metadata in
the response."

**B9. Story 9.12 — re-architecture of the parallel state.**
"after SoftDeleteAurora completes the workflow routes to a new HardPurgeNow
state" — but SoftDeleteAurora runs inside `ParallelCleanup` alongside two
sibling branches. If purge_immediately requires HardPurgeNow to follow
SoftDeleteAurora, the workflow needs either (a) a second `Choice` after
the parallel branch, or (b) a different state-machine shape where the
soft-delete path and the purge-immediately path diverge earlier (at
ValidateDeletionRequest). (b) is cleaner.
- **Recommend:** branch at ValidateDeletionRequest based on the input flag.
  - `purge_immediately=false` path: existing flow (Disable → Deactivate →
    Parallel{SoftDeleteAurora, DeleteDDBPersonal, AnonymizeChatMessages} →
    DeleteCognito → WriteAudit).
  - `purge_immediately=true` path: Disable → Deactivate → Parallel{
    SoftDeleteAurora → HardPurgeNow (Lambda from 9.11 scoped to one
    user_id), DeleteDDBPersonal, HardDeleteUserChatMessages (from 9.12)
    } → DeleteCognito → WriteAudit.
  Spell this state-machine fork explicitly in 9.1 and 9.12 acceptance
  criteria. Otherwise the subagent will invent it.

**B10. Story 9.13 — depends_on too narrow.**
Current `depends_on: [9.9, 9.6, 9.7, 9.4, 9.8]`. The E2E test asserts:
- Cognito user is **deleted** (UserNotFoundException) — requires the
  DeleteCognitoUser step (B1 resolution: 9.3 in "delete" mode).
- Aurora `users` row has `deleted_at` set with PII nulled — requires 9.5.
- (After B6/B7 wiring) the endpoint resolves correctly — requires 9.9.
- 9.10 isn't strictly required for the assertion, but the test polls
  status — depends_on should include 9.10.
- **Recommend** depends_on: `[9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10]`.

**B11. Story 9.11 — Aurora cascade verification.**
"The cascade ... is verified to fire." Verified how? Recommend the AC say:
"insert a `siblings` row for the test user pre-soft-delete; after the
hard purge, SELECT against `siblings` returns zero rows for that user_id."
Same for friendships, friend_requests, bookmarks, blocks — pick one per
table or assert all.

---

### C. Scope creep, drift, and external assumptions

**C1. NotifySuccess SNS + email is undefined.**
§13 item 20 (SES setup for transactional notifications) is unresolved.
Either drop NotifySuccess for v1, or defer it to the phase that adds SES
(none currently planned — likely phase 11 hardening). **Recommend drop**
and remove from the 9.1 state-machine list.

**C2. DeletionFailed alerting.**
The architecture lists "SNS alert to ops email" in the Catch. Without
SES/SNS-email subscription, this is a metric-only event. Recommend the
state machine writes a `deletion_failed` audit row (reusing 9.8) and emits
a CloudWatch metric; phase-10 wires the alarm + email subscription on top.
This keeps the failure path observable without expanding phase-9 scope.

**C3. AppSync onRoomDeactivated mutation publish (story 9.4).**
"publishes an AppSync mutation that fires onRoomDeactivated to the
surviving participant." Phase 8 ships `room_state_publisher` that
consumes ChatRooms DDB Streams and publishes the AppSync mutation. So
9.4 only needs to UpdateItem on ChatRooms; the stream consumer already
does the AppSync publish. **Recommend:** rewrite the AC to "UpdateItem on
each ChatRooms row; the existing room_state_publisher Lambda
(phase-8 story 8.9a) consumes the stream and fires onRoomDeactivated.
No new AppSync wiring in this story." This is drift, not a defect — but
the subagent will otherwise duplicate phase-8 work.

**C4. Cognito disable+delete cost of two Lambdas.**
Two Cognito Lambdas (Disable in 9.3, Delete in B1) is wasteful — single
Lambda with a `mode` input is cheaper and idempotent. **Recommend** the
fold into 9.3.

**C5. Audit table not in dynamodb module today.**
9.8 says "added to the dynamodb module." Confirm the convention: every
table lives there. `account_deletion_audit` PK=user_id, SK=event_id with
TTL+ Number attribute. The dynamodb module is already large; that's fine.

**C6. Lambda layer for psycopg / boto3.**
Stories that touch Aurora (9.5, 9.11, 9.12) need the `db` layer (already
exists at `infrastructure/src/layers/db/`). Spell out in 9.5/9.11/9.12
that the Lambda uses the existing db layer and gets credentials from the
Secrets Manager rotation pattern used by all aurora-writers — no new
secret machinery.

**C7. IAM roles.**
Every Lambda needs an IAM role declared in `infrastructure/modules/iam_roles/`.
This is implicit in every phase, but past phases have hit IAM-role gaps
late (43/43 TF tests is the watermark). Suggest a single closing AC on
the phase: "iam_roles module has one role per new Lambda, all TF tests
green, IAM scoping is least-privilege per the action lists in
architecture §11.2." Could be added to 9.1 or as a 9.0-style closeout.

**C8. Step Functions execution role.**
The state machine itself needs an IAM role with `lambda:InvokeFunction` on
each task Lambda ARN. Story 9.1 should explicitly include this in the AC,
otherwise the subagent will write the state machine and forget the
execution role. (Likely will catch in TF plan, but worth pre-empting.)

**C9. CloudFront origin secret + WAF passthrough.**
DELETE /v1/profile/me must go through CloudFront like every other API
route. The WAF allowlists JSON content types on POST/PUT/PATCH/DELETE; a
DELETE with no body should not be blocked. No change expected but worth
sanity-checking the WAF rules during 9.9.

**C10. Deletion of a user with no chat rooms / no messages / no tokens.**
Every step must be idempotent on empty inputs. The PRD generally says
this, but make it a phase-wide acceptance criterion or check it per story.
9.4 with zero rooms, 9.6 with zero messages, 9.7 with zero notifications
+ zero tokens — all should succeed-as-noop.

---

### D. Recommended PRD edits, summary

If the user picks `address`, here is the minimum PRD diff:

1. **Story 9.1** — add ACs: "Step Functions execution role declared in
   iam_roles with `lambda:InvokeFunction` on every task Lambda";
   "ValidateDeletionRequest output includes a `purge_immediately` branch
   key consumed by a top-level Choice state"; drop NotifySuccess; keep
   `DeletionFailed` as a Catch that writes a `deletion_failed` audit row
   and emits a CloudWatch metric.
2. **Story 9.3** — extend AC: "Lambda accepts `mode` input
   ('disable' | 'delete') and dispatches accordingly. State machine wires
   it twice: as DisableCognitoUser early and DeleteCognitoUser late."
3. **Story 9.4** — extend AC: "Also DeleteItem on the deleted user's
   ChatRoomMembership rows (PK=user_id Query → BatchWriteItem deletes).
   Surviving participants' rows untouched." Drop "publishes an AppSync
   mutation" — phase-8 room_state_publisher consumes the stream and does
   this. Update integration test to assert membership cleanup.
4. **Story 9.5** — no semantic change; spell out idempotency conditional.
5. **Story 9.6** — replace "Scan or GSI" with "Query ChatRoomMembership
   PK=user_id for room_ids, then Query ChatMessages by room_id with
   filter on sender_id, UpdateItem to anonymize." Drop the "may add a
   GSI" sentence. Rewrite the continuation-token loop as Choice→Task→
   Choice rather than Map.
6. **Story 9.7** — unchanged.
7. **Story 9.8** — TTL attribute is `expire_at` (Number, Unix epoch
   seconds); writer computes the value. Add the
   `dynamodb_retention: "permanent_anonymized"` (or
   `"hard_deleted"` in the purge_immediately path) field to the
   "deletion_completed" record.
8. **Story 9.9** — path corrected to `infrastructure/src/functions/...`;
   DELETE integration is a separate aws_apigatewayv2_integration; profile
   Lambda is unaffected.
9. **Story 9.10** — unchanged (small wording tightening).
10. **Story 9.11** — path corrected; cascade verification spelled out
    (assert one row in each cascading table is gone post-purge).
11. **Story 9.12** — re-architect the purge_immediately branch as a
    top-level Choice at ValidateDeletionRequest (not a follow-on after
    SoftDeleteAurora). Include `HardDeleteUserChatMessages` (replaces
    AnonymizeChatMessages on this branch) and DeleteItem on
    ChatRoomMembership. Integration test asserts the messages are gone,
    not just anonymized.
12. **Story 9.13** — `depends_on: [9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10]`.
    Add asserts: deleted user's ChatRoomMembership rows are empty;
    surviving user's row is present; block_filter regression scenario
    from oq-9.C.

---

### E. What I'm NOT flagging

- `purge_immediately` API surface (how the mobile client sets it). The PRD
  says it's a body flag default false on DELETE /v1/profile/me. Fine.
- The 30-day window vs. 7-day backup retention. Already addressed by
  §11.3; not a phase-9 concern.
- Cost (state machine + Lambdas at <100 deletions/month — negligible).

---

### F. Suggested resolution path

The list looks long but most edits are 1-2 lines per story. The
non-trivial ones are:

- 9.1 / 9.12: state-machine restructuring (purge_immediately as a
  top-level Choice). This is the only architectural change.
- 9.6: query strategy (no GSI, use ChatRoomMembership as the iterator).
- 9.4: membership-delete + drop redundant AppSync publish.

Everything else is path corrections, AC tightening, depends_on fix-up.

**Question for user:** address these gaps in the PRD and re-run
`/implement-phase 9`, or proceed with the current PRD?

---

## 2026-06-20 11:45 brainstorm (verification pass)

Re-running brainstorm against the rewritten PRD (last_updated 2026-06-20)
to verify the prior concerns were closed and surface any new gaps the
restructure introduced. Reading the PRD end-to-end as if I had not been
the one who edited it.

### G. Prior concerns — closed?

Every item from the 2026-06-19 pass cross-checked against the current PRD:

| Item | Status |
|------|--------|
| oq-9.A — membership cleanup | ✅ Folded into 9.4 AC + 9.13 assertions |
| oq-9.B — DDB permanent retention | ✅ `dynamodb_retention` field on audit row, 9.8 |
| oq-9.C — block_filter safety | ✅ Regression sub-test in 9.13 |
| oq-9.D — purge_immediately DDB cleanup | ✅ HardDeleteUserChatMessages in 9.12 |
| B1 — undefined state-machine steps | ✅ 9.3 mode param; NotifySuccess dropped; DeletionFailed via metric+audit |
| B2 — Map vs Choice loop | ✅ Choice → Task → Choice spelled out in 9.6 and 9.12 |
| B3 — GSI placement | ✅ No GSI; ChatRoomMembership-driven query |
| B5 — TTL unit | ✅ Number Unix epoch seconds, formula given |
| B6 — file path typos | ✅ Corrected in 9.9, 9.11, 9.12 |
| B7 — DELETE collision | ✅ Separate aws_apigatewayv2_integration spelled out |
| B8 — JWT verification | ✅ Spelled out in 9.10 + no-leak-on-403 |
| B9 — workflow restructure | ✅ Top-level Choice on purge_immediately in 9.1 |
| B10 — 9.13 depends_on | ✅ Expanded (but see new gap H4 below) |
| B11 — cascade verification | ✅ Spelled out in 9.11 |
| C1 — NotifySuccess dropped | ✅ |
| C2 — DeletionFailed alerting | ✅ Audit row + CloudWatch metric, SNS deferred to phase 10 |
| C3 — AppSync duplicate | ✅ Removed from 9.4 with explicit reference to 8.9a |
| C4 — two Cognito Lambdas | ✅ Folded into 9.3 with mode param |
| C5 — audit table location | ✅ `infrastructure/modules/dynamodb/` |
| C6 — db layer reuse | ✅ Spelled out in 9.5, 9.11; mentioned in 9.12 |
| C7/C8 — IAM roles | ✅ Phase-wide in context_summary + execution role in 9.1 |
| C9 — WAF passthrough | ✅ Sanity check note in 9.9 |
| C10 — idempotency on empty | ✅ Phase-wide AC + 9.6 empty-input test |

All prior items addressed. ✅

---

### H. New gaps introduced by the rewrite

**H1 — CRITICAL: ordering contradiction between 9.4 and 9.6.**

The state machine in 9.1 specifies (soft-delete branch):
`... → DeactivateChatRooms → ParallelCleanup{SoftDeleteAurora,
DeleteDynamoDBPersonalData, AnonymizeChatMessages} → ...`

Story 9.4 (DeactivateChatRooms) now ALSO deletes the user's
ChatRoomMembership rows (resolution of oq-9.A).

Story 9.6 (AnonymizeChatMessages) iterates ChatRoomMembership PK=user_id
to collect room_ids, then queries each room for the user's messages.

By the time AnonymizeChatMessages runs (inside ParallelCleanup), 9.4
has already deleted the membership rows. The Query returns zero rows.
AnonymizeChatMessages exits with has_more=false having done nothing.
**No messages would be anonymized.** This is a real, ship-blocking bug
that would only surface in E2E tests.

The 9.6 AC even acknowledges this: "this Lambda must run BEFORE story
9.4 deletes the user's ChatRoomMembership rows, OR receive the room_id
list from the orchestrator." But the state-machine wiring in 9.1
implements neither option.

Fix options (pick one):
- **Option A:** State machine passes the room_id list from 9.4's
  output as input to AnonymizeChatMessages (and to
  HardDeleteUserChatMessages in the purge_immediately branch).
  9.4 emits `{room_ids: [...]}` in its result; ParallelCleanup uses
  Step Functions Parameters/ResultPath to feed those into 9.6/9.12.
  Cleanest — keeps 9.4 as one Lambda.
- **Option B:** Split 9.4 into 9.4a (deactivate ChatRooms only, run
  early) and 9.4b (delete membership rows, run AFTER the parallel
  block). More Lambdas, simpler data flow.
- **Option C:** Move AnonymizeChatMessages BEFORE DeactivateChatRooms
  in the state machine, outside the parallel block. Makes the
  parallel block do less work — defeats the "do cleanup in parallel"
  intent.

**Recommend Option A.** Spell out in 9.1 that DeactivateChatRooms
emits `{room_ids}` in its output and ParallelCleanup uses an explicit
Parameters block to inject room_ids into AnonymizeChatMessages
(soft-delete branch) and HardDeleteUserChatMessages (purge_immediately
branch). Strike the "AnonymizeChatMessages performs its own Query on
ChatRoomMembership at the start of its first invocation" recommendation
from 9.6 — it's incompatible with the state-machine ordering.

**H2 — 9.2 doesn't pass purge_immediately through.**

The state machine forks on `input.purge_immediately` at a top-level
Choice immediately after ValidateDeletionRequest (9.1 AC). For that
to work, 9.2's output must preserve `purge_immediately` (Step Functions
defaults to passing the task result as the next state's input, so the
Lambda needs to either echo it back or use ResultPath to merge with
input).

The 9.2 AC doesn't mention this. Easy gap to close: add an AC that the
Lambda's output preserves the `purge_immediately` flag from input, OR
declare in 9.1 that the Choice uses `States.JsonPath` against
`$.purge_immediately` with a ResultPath wiring on 9.2 that preserves
the original input.

**Recommend:** add to 9.2 AC: "the Lambda's response echoes
`purge_immediately` from input verbatim so the downstream Choice can
branch on it. Alternatively, the state-machine wiring for
ValidateDeletionRequest uses `ResultPath: '$.validation'` so the
original input.purge_immediately survives unchanged."

**H3 — 9.11 per-user mode loses the deleted_at safety check.**

9.11 AC line 3: "When invoked with a user_id, the WHERE clause is
`WHERE user_id = :id` (the deleted_at age check is skipped because
purge_immediately bypasses the 30-day window)."

This drops BOTH the age check AND the `deleted_at IS NOT NULL` guard.
In the purge_immediately branch, SoftDeleteAurora runs first and sets
deleted_at, so the guard would hold in practice. But removing it makes
9.11 a hard-delete-any-user-by-id Lambda, which is a footgun if anyone
ever invokes it without first running SoftDeleteAurora.

**Recommend:** per-user mode WHERE clause is
`WHERE user_id = :id AND deleted_at IS NOT NULL` (keep the soft-delete
guard, drop only the age check). If the user hasn't been soft-deleted,
the Lambda returns rows_affected=0 — safer.

**H4 — 9.13 depends_on still missing 9.11 and 9.12.**

The 2026-06-19 brainstorm recommended `[9.3, 9.4, 9.5, 9.6, 9.7, 9.8,
9.9, 9.10]`. The rewrite applied that. But the 9.13 AC now ALSO
includes:
- "block_filter regression scenario ... A is hard-purged via 9.11" →
  needs 9.11
- "purge_immediately variant ... C calls DELETE with
  purge_immediately=true" → needs 9.12

**Recommend:** expand to
`[9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12]`. 9.13
genuinely depends on everything in the phase.

**H5 — 9.12 has a contradictory db-layer sentence.**

9.12 AC line 3: "Uses the existing db layer if Aurora access is
needed; this Lambda is DDB-only so no db layer required."

The conditional ("if Aurora access is needed") followed by the
absolute ("DDB-only so no db layer required") is confusing. Pick one.

**Recommend:** simplify to "DDB-only Lambda; no db layer required."

**H6 — HardPurgeNow inside the parallel block is a partial-failure
risk worth acknowledging.**

In the purge_immediately branch, ParallelCleanup contains
`SoftDeleteAurora → HardPurgeNow` chained inside one arm, with the
other two arms running concurrently. HardPurgeNow runs `DELETE FROM
users` which cascades — once it fires, Aurora data is gone permanently.
If `HardDeleteUserChatMessages` (sibling arm) fails after HardPurgeNow
succeeded, the failure handler writes a `deletion_failed` audit row
but Aurora cannot be restored. The next retry would find the user
already gone from Aurora.

This is acceptable risk (the user explicitly asked for immediate
purge; partial DDB cleanup on retry is recoverable since 9.12 is
idempotent), but should be acknowledged. The HardDeleteUserChatMessages
Lambda needs to handle "user already gone from Aurora" gracefully —
it shouldn't need Aurora access at all, so this is fine in practice.

**Recommend:** no PRD change required, but add a one-line note to
9.12 that HardDeleteUserChatMessages must not depend on Aurora state
(idempotent regardless of whether Aurora row still exists).

---

### I. Severity summary

- **Ship-blockers:** H1 (real bug — no messages would get anonymized).
- **Strongly recommended:** H2 (state-machine wiring won't work
  without it), H4 (test will fail without dependencies), H5
  (cosmetic but a reviewer round-trip).
- **Nice to have:** H3 (safety hardening), H6 (documentation).

### J. Minimum PRD diff to fix

1. **Story 9.1** — add to ACs: "DeactivateChatRooms emits `room_ids`
   in its result; ParallelCleanup uses explicit Parameters block to
   inject `room_ids` into AnonymizeChatMessages (soft-delete branch)
   and HardDeleteUserChatMessages (purge_immediately branch). The
   top-level Choice on `purge_immediately` consumes the field from
   ValidateDeletionRequest's preserved input (use ResultPath to
   merge)."
2. **Story 9.2** — add AC: "the Lambda's output echoes
   `purge_immediately` from input verbatim (or 9.1 uses ResultPath
   to preserve the original input.purge_immediately past this
   task)."
3. **Story 9.4** — add AC: "Lambda response includes
   `{room_ids: [list of room_ids the user was in]}` so downstream
   tasks can use them after the membership rows are deleted."
4. **Story 9.6** — strike the "performs its own Query on
   ChatRoomMembership" recommendation. Replace with: "receives
   `room_ids` as input from the state-machine Parameters block;
   iterates each room_id to find messages where sender_id matches."
5. **Story 9.11** — change per-user WHERE to
   `WHERE user_id = :id AND deleted_at IS NOT NULL`.
6. **Story 9.12** — replace "Uses the existing db layer if Aurora
   access is needed; this Lambda is DDB-only so no db layer required"
   with "DDB-only Lambda; no db layer required. Idempotent regardless
   of whether the Aurora users row still exists when invoked."
   Update HardDeleteUserChatMessages to "receives `room_ids` as
   input from the state-machine Parameters block" (mirrors 9.6).
7. **Story 9.13** — expand depends_on to
   `[9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12]`.

---

### K. Question for user

H1 is a real bug that would only surface at E2E test time. The other
items are small. Two options:

- **address** — apply the J diff above (≈15 minutes of edits), then
  re-run `/implement-phase 9`. Recommended.
- **proceed** — dispatch with the current PRD. The first developer
  on story 9.4 or 9.6 will hit H1 and either invent an ad-hoc fix
  (likely Option C — move AnonymizeChatMessages before
  DeactivateChatRooms, which is the worst option) or block.

---

## 2026-06-20 12:30 brainstorm (third pass — J-diff verification)

The J-diff from the 11:45 pass was applied. Re-reading the updated PRD
to verify the fixes are internally consistent and to surface any
remaining gaps before dispatch. Tracing data flow end-to-end.

### L. J-diff items — all applied?

| J-diff item | Status |
|-------------|--------|
| J1 — 9.1 ResultPath '$.validation' for 9.2 + Parameters block for room_ids | ✅ |
| J2 — 9.2 echo / ResultPath note | ✅ |
| J3 — 9.4 emits `{room_ids:[...]}` | ✅ |
| J4 — 9.6 receives `{user_id, room_ids}` as input, no own Query | ✅ |
| J5 — 9.11 per-user mode keeps `AND deleted_at IS NOT NULL` | ✅ |
| J6 — 9.12 DDB-only Lambda, idempotent regardless of Aurora state | ✅ |
| J7 — 9.13 depends_on expanded to include 9.11, 9.12 | ✅ |

All J-diff items applied verbatim. ✅

### M. Data-flow trace (after J-diff)

Walking the state machine input field-by-field:

1. **9.9 initiator** → `StartExecution(input = {user_id, purge_immediately})` ✅
2. **9.2 ValidateDeletionRequest** with `ResultPath: '$.validation'` → state becomes `{user_id, purge_immediately, validation: {...}}` ✅
3. **Choice on `$.purge_immediately`** → branches ✅
4. **9.3 DisableCognitoUser** needs `{user_id, mode: 'disable'}` — see N1 below
5. **9.4 DeactivateChatRooms** needs user_id, emits `{room_ids:[...]}` — see N2 below
6. **ParallelCleanup arms:** all need user_id from `$.user_id`; 9.6/9.12 also need `room_ids` via Parameters
7. **9.3 DeleteCognitoUser** needs `{user_id, mode: 'delete'}` — same as N1
8. **9.8 WriteAuditLog** needs user_id, execution_arn, etc. — implicit via state

The H1 ordering bug is fully resolved by the Parameters wiring. ✅

### N. New gaps surfaced this pass

**N1 — Dispatch-order bug: 9.2 missing depends_on [9.8].** SHIP-BLOCKER.

Story 9.2 writes a `deletion_initiated` audit record. That audit table is
created in story 9.8. Story 9.2 currently has `depends_on: []`, so it
could be dispatched before 9.8. The Lambda would deploy fine but fail at
runtime when the table doesn't exist — and worse, the integration test
in 9.2 ("a second invocation ... returns DeletionInProgress") implicitly
needs the audit table to read/write idempotency state.

**Fix:** `depends_on: [9.8]` on story 9.2.

**N2 — 9.9 and 9.10 integration tests assert end-to-end behavior but depends_on doesn't cover it.** SHIP-BLOCKER.

- 9.9's AC: "poll DescribeExecution until SUCCEEDED, assert users row has deleted_at set". This requires the full state machine (9.2 through 9.8) to be working. Current `depends_on: [9.1]` only.
- 9.10's AC: "poll the endpoint during a running deletion, observe state transitions". Same problem — a running deletion needs every step Lambda. Current `depends_on: [9.9]`.

Two ways to fix:
- (a) Expand depends_on to `[9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8]` for 9.9 and add 9.11/9.12 implicitly via 9.13.
- (b) Weaken the integration tests in 9.9 and 9.10 to only assert API-level behavior (202 returned, executionArn returned, status endpoint reachable). Move the end-to-end assertion to 9.13 only.

**Recommend (b)** — separation of concerns. 9.9 is about the HTTP endpoint that starts the workflow; 9.10 is about the status endpoint. Workflow correctness lives in 9.13. This also lets 9.9/9.10 be dispatched and validated earlier in the topo order rather than blocking on every Lambda.

Concrete edits:
- 9.9 integration test → "signup a user, call DELETE /v1/profile/me through CloudFront, observe 202 response with a valid executionArn, assert the state machine has an execution in RUNNING state (use ListExecutions). Do NOT poll for SUCCEEDED here — that assertion lives in 9.13."
- 9.10 integration test → "start a stub Step Functions execution (or use a real execution from a prior test), call GET /v1/profile/me/deletion-status?executionArn=..., assert response contains status field. Negative test: signup a second user, attempt to read the first user's executionArn with the second user's JWT, assert 403 and an empty body." (Negative test is the meaningful one; status retrieval against a stub execution proves the endpoint shape.)

**N3 — 9.1 has no Lambda depends_on but the AC references every Lambda ARN.** NON-BLOCKER (acceptable if module-as-variables pattern is used).

Story 9.1 AC: "Each task references the matching Lambda ARN from the stories below". With `depends_on: []`, 9.1 dispatches first. If the state-machine module declares Lambda ARNs as input variables (the standard pattern Knotify already uses for other multi-Lambda modules), this is fine — root TF wires the actual ARNs at deploy. The integration test on the state-machine module proper can run with stub ARNs or be deferred to 9.13.

**Recommend:** add a note to 9.1 AC: "Lambda ARNs are declared as module input variables (`var.lambda_arns.validate_deletion_request`, etc.); root TF in environments/ supplies the actual ARNs from each Lambda module's outputs after those Lambdas exist. Module-level TF tests pass with stub ARN strings."

**N4 — Mode parameter injection for 9.3 not explicit.** MINOR.

The state-machine task for DisableCognitoUser needs Parameters like
`{"user_id.$": "$.user_id", "mode": "disable"}`. Same for DeleteCognitoUser
with `"mode": "delete"`. The 9.1 AC mentions "wires this Lambda twice" but
doesn't spell out the Parameters mechanism. Subagent will figure it out;
worth one-line nudge.

**Recommend:** add to 9.1 AC: "DisableCognitoUser and DeleteCognitoUser task definitions each use a Parameters block to inject `{user_id, mode}` into the task input (mode value is task-local, not from state)."

**N5 — 9.4's ResultPath not specified.** MINOR.

For the Parameters block in 9.1 to reference `room_ids` from 9.4's output,
9.4 needs `ResultPath: '$.deactivate_chat_rooms'` (or similar) so room_ids
land at a known path. Subagent will pick a path; worth pinning so downstream
tasks know what to reference.

**Recommend:** add to 9.1 AC: "DeactivateChatRooms is wired with `ResultPath: '$.deactivate_chat_rooms'`; the Parameters block on AnonymizeChatMessages and HardDeleteUserChatMessages references `$.deactivate_chat_rooms.room_ids`."

### O. Severity summary

- **Ship-blocker:** N1 (9.2 missing depends_on 9.8), N2 (9.9/9.10 test scope vs deps).
- **Strong recommend:** N3 (9.1 module-as-variables note).
- **Minor:** N4, N5 (wiring nudges — subagent can infer).

### P. Minimum PRD diff

1. Story 9.2 — `depends_on: [9.8]`.
2. Story 9.9 — rewrite integration test to API-level only (no polling for SUCCEEDED); keep `depends_on: [9.1]`.
3. Story 9.10 — rewrite integration test to use stub execution or pre-existing execution; keep the negative test as primary; keep `depends_on: [9.9]`.
4. Story 9.1 — add ACs covering the Lambda-ARN variable pattern (N3), the mode Parameters block (N4), and the ResultPath for 9.4 (N5).

### Q. Question for user

- **address** — apply the P diff (~10 minutes), re-run `/implement-phase 9`. Recommended.
- **proceed** — dispatch with current PRD. N1 will cause 9.2's dispatch to fail or be deferred manually. N2 will cause 9.9/9.10 to fail their integration tests until 9.13 is ready.

---

## 2026-06-20 12:55 brainstorm (fourth pass — P-diff verification)

P-diff applied. This pass is a final integrity check before dispatch.
Tracing data flow + dispatch order + remaining nits.

### R. P-diff items applied?

| P-diff item | Status |
|-------------|--------|
| P1 — 9.1 explicit ResultPath '$.deactivate_chat_rooms' + Parameters block for room_ids | ✅ |
| P2 — 9.1 explicit Parameters block for mode on Cognito tasks | ✅ |
| P3 — 9.1 Lambda-ARN-as-variable pattern note | ✅ |
| P4 — 9.2 depends_on [9.8] | ✅ |
| P5 — 9.9 integration test scoped to API-level only | ✅ |
| P6 — 9.10 integration test scoped to API-level + negative test | ✅ |

All P-diff items applied verbatim. ✅

### S. Dispatch order — topo-sort trace

Serial execution, one round per topo wave:

**Wave 1** (eligible immediately, deps=[]):
- 9.1, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8

**Wave 2** (newly eligible after wave 1 done):
- 9.2 (deps: [9.8])
- 9.9 (deps: [9.1])
- 9.11 (deps: [9.5])

**Wave 3**:
- 9.10 (deps: [9.9])
- 9.12 (deps: [9.1, 9.4, 9.5, 9.6, 9.11])

**Wave 4**:
- 9.13 (deps: [9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12])

All 13 stories reachable. No cycles. ✅

### T. Remaining cosmetic gaps (NON-BLOCKING)

**T1 — 9.13 depends_on doesn't list 9.1 or 9.2 explicitly.**
The E2E test in 9.13 needs both the state machine (9.1) and the validation
Lambda (9.2). In practice, dispatch order works correctly: 9.13's listed
deps include 9.9 (which depends on 9.1) and 9.8 (the only dep 9.2 has),
so by the time wave 4 begins, both 9.1 and 9.2 are already done.

This is a transitive-dependency completeness nit, not a functional bug.
Add for clarity if convenient; safe to leave as-is.

**T2 — No story explicitly tests the Catch path (deletion_failed).**
The 9.1 AC says the Catch writes a deletion_failed audit row and emits
a CloudWatch metric. No story tests this. To test it, you'd need a Lambda
that throws on purpose, which means injecting failure into an otherwise
healthy workflow. Worth a 14th story (or AC bullet in 9.13) but acceptable
to defer — phase 10 (observability) is the natural home for "alarm fires
when DeletionFailed metric increments."

Recommendation: leave as-is for phase 9; ensure phase 10 includes a
"DeletionFailed metric → CloudWatch alarm + ops email" story.

### U. Verdict

PRD is dispatch-ready. The four-round brainstorm process surfaced and
resolved every concrete ordering, contradiction, and dependency gap.
The remaining T1/T2 items are cosmetic / scope-deferred respectively.

**Recommend proceed.**

---

## 2026-06-20 14:00 brainstorm (fifth pass — dispatch-eve final integrity check)

Triggered by `/implement-phase 9` invocation. Re-reading the PRD as if for the
first time and validating against repo HEAD (post phase-8 closeout).

### V. Repo state cross-check

| PRD assumption | Repo state | Verdict |
|----------------|------------|---------|
| `infrastructure/modules/dynamodb/` exists and owns every table | confirmed (main.tf, outputs.tf, variables.tf, tests/) | ✅ |
| `infrastructure/modules/iam_roles/` is where per-Lambda roles land | confirmed | ✅ |
| `infrastructure/src/layers/db/` exists for Aurora-touching Lambdas | confirmed (build.sh, knotify_db/, layer_manifest.txt) | ✅ |
| `infrastructure/src/functions/` has 17 existing handlers; new ones for phase 9 (deletion_initiator, hard_purge, hard_delete_user_chat_messages, etc.) do not yet exist | confirmed (only existing handlers; no name collisions) | ✅ |
| `room_state_publisher` (phase-8 8.9a) consumes ChatRooms DDB stream and publishes onRoomDeactivated → 9.4 must NOT duplicate this | confirmed Lambda + module + tests all present | ✅ |
| `profile` Lambda owns GET/PUT /v1/profile/me; DELETE is unused | confirmed (only GET/PUT) — DELETE integration in 9.9 is greenfield | ✅ |
| `account_deletion_audit` table does NOT yet exist in dynamodb module | confirmed absent — 9.8 creates it | ✅ |
| ChatMessages has no GSI on sender_id | confirmed (only the existing PK/SK plus phase-8 GSIs) — 9.6 query strategy is correct | ✅ |
| Aurora ON DELETE CASCADE on users for siblings/friendships/friend_requests/bookmarks/blocks | confirmed earlier via migrations 0003-0006 (no change since) | ✅ |

No drift since phase-8 closeout. PRD assumptions hold.

### W. Final integrity sweep

Re-read every story's acceptance_criteria once more, looking for the
classes of bug that survive four passes:

- **Story 9.1** — state-machine module pattern matches the existing
  `appsync` and `room_state_publisher` modules (input-variable ARN
  injection, dedicated TF tests with stubs). Convention preserved. ✅
- **Story 9.2** — depends_on [9.8] is correct (audit table must exist
  before the validator writes to it). ResultPath wiring is spelled out
  in 9.1, so 9.2 doesn't need to echo input. ✅
- **Story 9.3** — single Lambda, two modes. Idempotency on
  UserNotFoundException + already-disabled clearly stated. ✅
- **Story 9.4** — ChatRooms UpdateItem + membership BatchWriteItem
  delete + room_ids in response. AppSync publish explicitly excluded
  (phase-8 stream consumer handles it). ✅
- **Story 9.5** — UPDATE conditional on `deleted_at IS NULL` (re-run
  noop). db layer + Secrets Manager pattern explicit. ✅
- **Story 9.6** — receives `{user_id, room_ids}`; no own membership
  Query. Choice→Task→Choice continuation loop spelled out. Empty-input
  test included. ✅
- **Story 9.7** — Notifications + PushNotificationTokens delete.
  Idempotent. ✅
- **Story 9.8** — `expire_at` Number (Unix epoch seconds) with formula.
  `dynamodb_retention` field on completion record. failed-record fields
  enumerated. ✅
- **Story 9.9** — separate aws_apigatewayv2_integration (profile Lambda
  untouched). WAF passthrough sanity check. API-level test only (no
  SUCCEEDED polling — that's 9.13's job). ✅
- **Story 9.10** — JWT-vs-execution-input authorization with no-leak
  403 body. Negative test is primary assertion. ✅
- **Story 9.11** — daily scheduled rule. Per-user mode keeps
  `AND deleted_at IS NOT NULL` guard. Cascade verification across all
  5 child tables. ✅
- **Story 9.12** — HardDeleteUserChatMessages mirrors 9.6's input
  contract. DDB-only, idempotent regardless of Aurora state. Parallel
  cleanup wiring correct. ✅
- **Story 9.13** — depends_on covers 9.3–9.12 (transitively covers
  9.1/9.2). Three sub-tests: soft-delete E2E, block_filter
  regression, purge_immediately variant. ✅

### X. Concerns I deliberately did NOT flag (acceptable trade-offs)

- **No load test on 9.6 / 9.12** — fine for v1 (no users with >100k
  messages yet; continuation token absorbs the long tail).
- **No explicit Catch-path test** — covered by future phase 10 alarm
  story per T2 from prior pass.
- **No mobile-client UX coverage** — out of scope; backend phase only.
- **No phase-9 SES/email** — explicitly deferred per architecture §13
  item 20 and B1 resolution.

### Y. Dispatch readiness verdict

PRD is dispatch-ready. **Recommend proceed.**

The four prior passes plus this integrity sweep have surfaced and
resolved every concrete issue I can identify. Re-running the brainstorm
again would yield nothing new.

