hotfix: friend-request-correctness
title: Design — how to implement the friend-request correctness hotfix
created: 2026-11-19
owner: backenddeveloper
companion_to: implementationplan/friend-request-correctness.md
status: approved

# Design

This document is the "how" layer on top of the PRD
(`implementationplan/friend-request-correctness.md`, which covers "what & why").
It records the decisions that shape the implementation so a reviewer can
reconstruct intent from the diff.

## Scope and non-goals

Seven stories (FR-1 … FR-7) are executed on a single hotfix branch,
`hotfix/friend-request-correctness`, with one commit per story and one PR
into `development`.

| Story | Touches | Why |
|---|---|---|
| FR-1 | `friends/handler.py` GET + SQL | Honour `direction` query param |
| FR-2 | `friends/handler.py` POST | 409 `reverse_pending` |
| FR-3 | `friends/handler.py` POST | 409 `already_friends` |
| FR-4 | tests only (verification) | Prove `already_pending` path |
| FR-5 | `friends/handler.py` POST + IAM + TF env | Write Notifications row on create |
| FR-6 | `friends/handler.py` accept | Write Notifications row on accept |
| FR-7 | new `test_friend_requests_e2e.py` | One happy-path live-AppSync test |

Non-goals:
- No refactor of `_set_friendship_active` (unrelated DynamoDB producer that
  already works). The new Notifications PutItem lives alongside it, not inside
  it.
- No changes to `notifications_publisher` (downstream consumer; already handles
  both `friend_request_received` and `friend_request_accepted` per lines
  158-188).
- No decomposition of `handler.py` into multiple modules. It grows by ~150
  lines; extraction would add churn without reuse benefit.
- No global "Aurora → Postgres" rename in docstrings. Minor cleanup is in
  scope only for docstrings we are already touching for this hotfix.

## The two-store write pattern (unchanged)

The friends handler already uses a "Postgres-first, DynamoDB best-effort"
pattern for its existing chat-room-active side effect (handler.py:585-593,
`_set_friendship_active`). FR-5 and FR-6 extend the same pattern to the
Notifications table write:

1. Begin Postgres transaction via `knotify_db.rls_context(conn, user_id, user_sex)`.
2. Run all pre-flight 409 checks (block / already-friends / reverse-pending)
   inside the transaction so concurrent inserts cannot race through.
3. INSERT / UPDATE and COMMIT (handled by `rls_context` context manager exit).
4. Then `_write_notification_received(...)` or `_write_notification_accepted(...)`
   via DynamoDB PutItem.
5. On PutItem failure: log the error, leave the Postgres row committed, and
   add `notification_pending: true` to the HTTP response body.

User-facing consequence of a PutItem failure: the friend request itself
succeeds, but the recipient's real-time subscription event is skipped. The
frontend's polling fallback (parallel branch
`fix/pre-send-and-incoming-polling`) picks it up on the next poll cycle. No
data loss, at worst a delayed modal.

## 409 precedence order on POST /v1/friend-requests

The three pre-flight 409 branches plus the UniqueViolation path run in this
order, and the order is load-bearing for UX:

1. **blocked** — first, so a blocker's state is not leaked by a different
   error code appearing earlier.
2. **already_friends** — second, so the caller sees "you're already friends"
   rather than "you already requested them" when both are true after an
   accept-then-request race.
3. **reverse_pending** — third, carries the reverse request's `request_id`
   so the frontend can offer "they also want to be friends — accept their
   request" UX.
4. **already_pending** — last, via `psycopg2.errors.UniqueViolation` on the
   INSERT. Covers the race where two concurrent POSTs both pass the SELECT
   pre-check; the UNIQUE constraint `(from_user_id, to_user_id, status)`
   catches the loser.

All three pre-flight SELECTs (`is_blocked` plus the two new ones) execute
inside the same `rls_context` transaction. The reverse-pending SELECT uses
`FOR UPDATE` so a concurrent reverse POST cannot race the accept-offer branch
through.

## SQL for the two new pre-flight checks

Added to the SQL constant block near the existing statements:

```python
# FR-2: reverse-pending check (inside the POST transaction, FOR UPDATE)
_SELECT_REVERSE_PENDING_FOR_UPDATE_SQL = """
SELECT request_id
FROM friend_requests
WHERE from_user_id = %s::uuid
  AND to_user_id   = %s::uuid
  AND status = 'pending'
FOR UPDATE
"""

# FR-3: already-friends check (inside the POST transaction)
# Uses canonical pair ordering (lex-min, lex-max) — matches _canonical_pair().
_SELECT_FRIENDSHIP_SQL = """
SELECT 1
FROM friendships
WHERE user_a = %s::uuid
  AND user_b = %s::uuid
"""
```

Both checks bind UUID parameters; both run inside the existing `rls_context`
transaction in `_handle_post_friend_requests`.

## The two new DynamoDB helpers (FR-5, FR-6)

Added next to `_set_friendship_active` / `_clear_friendship_active` with the
same shape:

```python
def _write_notification_received(recipient_id, requester_id, request_id,
                                 requester_first_name) -> bool:
    """Write a type='friend_request_received' row to Notifications. Best-effort."""
    # PutItem with the schema the notifications_publisher consumer expects:
    #   PK  user_id = recipient_id
    #   SK  created_at_notification_id = f"{iso}#{ulid}"
    #       notification_id = ulid
    #       type = 'friend_request_received'
    #       payload = json.dumps({"request_id", "from_user_id"})
    #       sender_user_id, sender_name
    #       read = False, delivered = False
    #       created_at = iso
    # Returns False on any DynamoDB error; True on success.

def _write_notification_accepted(original_sender_id, acceptor_id, request_id,
                                 acceptor_first_name) -> bool:
    """Write a type='friend_request_accepted' row to Notifications. Best-effort."""
    # Same schema; recipient is the ORIGINAL SENDER (not the acceptor).
```

ULID generation uses `ulid-py` if available (already a layer dep) or falls
back to `uuid.uuid4().hex` — any 26-char-ish string works; the SK just needs
to sort by creation time within a user.

Requester / acceptor first-name lookup happens via a single additional SELECT
on the `users` table inside the same Postgres transaction before COMMIT —
free data, no extra round trip after commit.

## IAM and Terraform changes (FR-5)

Add a new inline policy on the `friends_writer` role in
`infrastructure/modules/iam_roles/main.tf`, scoped to `dynamodb:PutItem` on
the Notifications table only:

```hcl
data "aws_iam_policy_document" "friends_writer_notifications" {
  statement {
    sid     = "NotificationsPutItem"
    effect  = "Allow"
    actions = ["dynamodb:PutItem"]
    resources = [
      var.notifications_table_arn != ""
        ? var.notifications_table_arn
        : "arn:aws:dynamodb:*:*:table/Notifications"
    ]
  }
}

resource "aws_iam_role_policy" "friends_writer_notifications" {
  name   = "friends-writer-notifications"
  role   = aws_iam_role.friends_writer.name
  policy = data.aws_iam_policy_document.friends_writer_notifications.json
}
```

Kept separate from the existing `friends_writer_dynamodb` (ChatRooms-only)
policy so reviewers see the scope expansion as its own diff.

`notifications_table_arn` is already a wired variable on the `iam_roles`
module (used by chat_resolver, notifications_publisher, push_fanout). No new
variables; no new module outputs.

Env var: add `TABLE_NOTIFICATIONS = module.dynamodb.notifications_table_name`
to the `friends` module in both `infrastructure/environments/dev/main.tf` and
`infrastructure/environments/prod/main.tf`.

## Test strategy

Per decision in brainstorm: per-story tests assert the DynamoDB row is
written (fast, deterministic via `GetItem` or Streams mock); a single
happy-path test in `test_friend_requests_e2e.py` exercises the live AppSync
subscription.

- **FR-1 unit/integration** → extend `test_friends.py`: incoming-only,
  outgoing-only, missing, invalid (400). Verify block_filter still applies to
  both columns.
- **FR-2 integration** → `test_friends.py`: reverse-pending triggers 409 with
  `error: reverse_pending` + the original `request_id`.
- **FR-3 integration** → `test_friends.py`: already-friends triggers 409 with
  `error: already_friends`.
- **FR-4 integration** → `test_friends.py`: same-direction duplicate triggers
  409 with `error: already_pending`. Pure verification; no code change
  expected unless the test reveals a gap.
- **FR-5 integration** → `test_friends.py`: POST succeeds → `GetItem` on
  Notifications table shows the row with the correct schema. No subscription
  assertion.
- **FR-6 integration** → `test_friends.py`: accept succeeds → `GetItem` on
  Notifications table shows the `friend_request_accepted` row. No
  subscription assertion.
- **FR-7 end-to-end** → new `test_friend_requests_e2e.py`: ONE happy-path
  test walks the full 9-step sequence from the PRD. The sub-assertions "A's
  phone receives the subscription event" and "B's phone receives the
  subscription event" each use the `_open_appsync_subscription` helper
  pattern from `test_room_state_publisher.py:116`. This test is skip-gated on
  `KNOTIFY_RUN_INTEGRATION=1` (existing convention).

Teardown is idempotent per the PRD: DELETE from `friendships`,
`friend_requests`, and `Notifications` for the two test user_ids, swallowing
not-found errors.

## Branch, commit, PR

- Base branch: `development` (current HEAD: `c873fee`).
- Hotfix branch: `hotfix/friend-request-correctness`.
- Commits, in order (one per story): FR-1, FR-2, FR-3, FR-4, FR-5, FR-6, FR-7.
  Plus the design-doc commit as commit 0.
- One PR into `development`, title:
  `hotfix: friend-request correctness — direction filter, pre-flight 409s, Notifications producer`.
- No cherry-pick to `main`. Phase 14's Supabase cutover is dev-only;
  production still runs Aurora. The handler code is backend-agnostic (uses
  `knotify_db.rls_context`), so this hotfix lands on `development` and
  promotes to `main` via the normal release flow once the surrounding
  phases are ready.

## Deployment ordering

Terraform and Lambda code ship in the same PR. The CD pipeline applies
Terraform first (creating the IAM grant + env var) then deploys the new
Lambda zip. If an operator applies manually: run `terraform apply` for the
`iam_roles` + env scope before invoking `deploy-lambda.sh friends`. Without
the IAM grant, the new Notifications PutItem returns 403 at runtime; the
`notification_pending: true` fallback catches it, so no data loss, but the
modal will not fire until the grant is in place.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Live-AppSync e2e test is flaky | Only one such test (FR-7); per-story tests use `GetItem` instead of waiting on websocket frames |
| Pre-flight SELECTs add latency to POST | Three indexed point lookups, all inside one transaction; measured impact < 10ms |
| DynamoDB PutItem fails silently | `notification_pending: true` in response + ERROR-level log; frontend polling fallback picks up the request |
| Reverse-pending check races a concurrent create | `FOR UPDATE` lock on the reverse row serializes any concurrent reverse POST |
| Lambda role expansion is too broad | New inline policy is scoped to `dynamodb:PutItem` on Notifications only; ChatRooms-only policy is unmodified |

## Done definition

When this PR merges:
- A friend request from A → B produces an IncomingRequestModal on B's phone.
- An accept from B → A produces a RequestAcceptedModal on A's phone.
- B's Requests tab shows only incoming rows (no self-rows via `direction=incoming`).
- Attempts to send a reverse / duplicate / already-friends request all return
  409 with distinct error codes.
- `test_friend_requests_e2e.py` passes against dev with
  `KNOTIFY_RUN_INTEGRATION=1`.
