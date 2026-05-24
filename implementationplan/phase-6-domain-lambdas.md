phase: 6
title: Profile, friends, bookmarks, blocks domain Lambdas
last_updated: 2026-05-24

context_summary: |
  Ships the first wave of business-logic Lambdas: knotify-profile, knotify-friends, knotify-bookmarks, knotify-blocks. Each Lambda derives user_id from the JWT sub (never from URL or body), sets the RLS session GUCs after authorizing, and uses the shared Aurora layer from phase 3. The corresponding REST routes (per §4.2 migration map) are wired through the HTTP API + JWT authorizer + CloudFront stack from phase 5. The stub /v1/_internal/hello endpoint from phase 5 story 5.7 is removed. Subsequent phases consume these domain Lambdas — chat (phase 8) calls friends and blocks logic to authorize room creation; match (phase 7) calls block lookups to filter results.

stories:
  - id: 6.1
    title: knotify-profile Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - src/functions/profile/ implements handlers for GET /v1/profile/me, PATCH /v1/profile/me, GET /v1/profiles?username=..., GET /v1/profiles/{userId}
      - PATCH validates the request body against a schema that rejects any of the immutable fields from §5.7 of architecture.md; an attempt to PATCH first_name returns HTTP 400 with a field list in the error body
      - GET /v1/profiles/{userId} returns only the deck-view subset of fields (§5.7); does not leak email, phone_number, family fields
      - Integration test against the dev Aurora cluster: signup two opposite-sex users, GET /v1/profile/me returns full profile, GET /v1/profiles/{otherId} returns only deck-view fields, GET on a same-sex user returns HTTP 404
    notes: ""

  - id: 6.2
    title: knotify-friends Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - src/functions/friends/ implements GET /v1/friends, DELETE /v1/friends/{userId}, GET /v1/friend-requests, POST /v1/friend-requests, POST /v1/friend-requests/{id}/accept, POST /v1/friend-requests/{id}/decline, DELETE /v1/friend-requests/{id}
      - POST /v1/friend-requests rejects with HTTP 409 when a pending request already exists between the same pair (enforced by the UNIQUE constraint from phase 2)
      - Accepting a request inserts a friendships row with canonical (user_a < user_b) and updates the friend_requests.status to 'accepted' in a single transaction
      - Integration test: A sends request to B, B accepts, GET /v1/friends from both sides shows the other user, A DELETE /v1/friends/{B} removes the friendship row
    notes: ""

  - id: 6.3
    title: knotify-bookmarks Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - src/functions/bookmarks/ implements GET /v1/bookmarks, POST /v1/bookmarks, DELETE /v1/bookmarks/{userId}
      - POST is idempotent — a second POST with the same userId returns 200 not 409
      - GET returns the denormalized profile info (deck-view fields) for each bookmarked user, joined through bookmarks → users
      - Integration test: bookmark two profiles, GET returns both, DELETE one, GET returns one
    notes: ""

  - id: 6.4
    title: knotify-blocks Lambda
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - src/functions/blocks/ implements GET /v1/blocks, POST /v1/blocks, DELETE /v1/blocks/{userId}
      - POST /v1/blocks rejects with HTTP 409 (reason NOT_FRIENDS) if no friendship row exists between blocker and target — blocking is only allowed against current friends (per owner decision 2026-05-24)
      - On POST, in a single Aurora transaction, the Lambda must (a) INSERT INTO blocks (blocker_id, blocked_id), (b) DELETE the friendship row for the canonical pair (auto-unfriend — per owner decision 2026-05-24), (c) DELETE any pending friend_requests rows between the two users in either direction
      - After the Aurora transaction commits, the Lambda must deactivate the chat room (if one exists) via a DynamoDB UpdateItem on ChatRooms keyed by room_id = sha256(canonical_pair(blocker, blocked)) — setting status='deactivated', deactivated_reason='blocked', deactivated_by=blocker_id, deactivated_at=NOW(). The UpdateItem must be conditional on attribute_exists(room_id) so it is a no-op when no room was ever created. Reactivation on unblock is the dual operation and is handled by DELETE /v1/blocks/{userId} per architecture §5.4.1
      - Integration test: A and B are friends, A blocks B → assert (i) friendships row gone, (ii) any pending friend_request between them gone, (iii) if a chat room existed, ChatRooms.status is 'deactivated' with deactivated_by=A. Then A POST /v1/friend-requests {toUserId: B} returns 409 with BLOCKED reason. A DELETE /v1/blocks/B succeeds and a follow-up POST friend-request now succeeds
      - Integration test: A is NOT friends with B, A POST /v1/blocks {userId: B} returns 409 NOT_FRIENDS and writes nothing to Aurora or DynamoDB
    notes: ""

  - id: 6.5
    title: HTTP API route wiring for all four Lambdas
    agent: backenddeveloper
    done: false
    depends_on: [6.1, 6.2, 6.3, 6.4]
    acceptance_criteria:
      - All routes from stories 6.1–6.4 are registered as aws_apigatewayv2_route resources with authorizer_id=Cognito JWT and an aws_apigatewayv2_integration target pointing at the correct Lambda
      - The stub /v1/_internal/hello route from phase 5 story 5.7 and its Lambda source are deleted from the repo
      - terraform plan in dev shows the new routes added and the hello route + function removed
    notes: ""

  - id: 6.6
    title: RLS session GUC enforcement integration tests
    agent: backenddeveloper
    done: false
    depends_on: [6.1, 6.2, 6.3, 6.4, 6.5]
    acceptance_criteria:
      - A new integration test suite tests/integration/rls_test.py drives the deployed dev API end-to-end and verifies opposite-sex enforcement at the DB layer
      - Test: a Male user calls GET /v1/profiles?username=<existing-male> and receives HTTP 404 even though the user exists, because RLS hides the row
      - Test: the same Male user calls GET /v1/profile/me and receives his own row (RLS exception path)
      - Test: a manual SQL query through the Lambda with the GUCs unset returns no rows for any other-sex user (confirming the policy defaults to deny)
    notes: ""

  - id: 6.7
    title: End-to-end suite for the four domains
    agent: backenddeveloper
    done: false
    depends_on: [6.5, 6.6]
    acceptance_criteria:
      - tests/integration/domains_e2e_test.py signs up two opposite-sex users via Cognito, then exercises: profile read/update, friend request send/accept, friendship list, bookmark add/list/remove, block/unblock affecting friend-request behavior
      - The entire suite passes against dev when invoked with "make test-e2e" and exits zero
    notes: ""
