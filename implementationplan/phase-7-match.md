phase: 7
title: Match and deck
last_updated: 2026-05-21

context_summary: |
  Implements the matching and swipe-deck endpoints backed by pgvector ranking and Postgres hard filters per §5.5 of architecture.md. The deck reads from the materialized deck_view created in phase 2 with the refresh strategy resolved in this phase. The match search joins pgvector cosine distance with hard filters and the blocks-exclusion subquery. The 20-D preference vector encoder is a shared utility. Subsequent phases do not depend on match (it's a leaf in the dependency tree of business logic), but the materialized-view refresh logic seeded here is also touched by phase 6's PATCH /v1/profile/me handler.

stories:
  - id: 7.1
    title: knotify-match POST /v1/match/search
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - src/functions/match/ handler for POST /v1/match/search accepts a body with filters {countries (list), religion, age_min, age_max} and returns up to 50 candidate profiles ordered by ascending pgvector cosine distance against the requester's preference_vector
      - The SQL query exactly mirrors §5.5 of architecture.md including the WHERE deleted_at IS NULL, profile_complete_verified=true, opposite-sex filter, and the NOT EXISTS subquery against blocks (both directions)
      - Integration test: seed five candidates with known preference vectors, request a search from a requester whose vector is closest to candidate C; assert C is first in the response
      - Integration test: seed a candidate C, then have the requester block C; the search no longer returns C
    notes: ""

  - id: 7.2
    title: knotify-match GET /v1/match/deck
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Handler for GET /v1/match/deck reads from deck_view (not users) for efficiency, applies the opposite-sex filter via RLS, excludes blocked users, and returns the next batch (pagination by cursor)
      - The handler accepts optional ?filters=<json> query param with the same structure as the search endpoint
      - Integration test: deck returns only profile_complete_verified=true users (asserted by seeding one verified and one unverified candidate, then asserting only the verified one appears)
    notes: ""

  - id: 7.3
    title: Preference vector encoder shared utility
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - A module src/shared/prefs.py exposes encode_prefs(prefs: dict) -> list[float] implementing the 20-key mapping from §5.5 of architecture.md verbatim
      - The PREFERENCE_KEYS list is the single source of truth; importing it in another module returns the same list
      - Unit test asserts that encode_prefs({}) returns 20 zeros, encode_prefs({"highlyeducated": True}) returns a vector with 1.0 in the first slot and 0.0 elsewhere
    notes: ""

  - id: 7.4
    title: deck_view refresh strategy
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - A scheduled aws_cloudwatch_event_rule fires src/functions/refresh_deck_view/ every 15 minutes; the handler executes SELECT refresh_deck_view() against Aurora
      - The PATCH /v1/profile/me handler in knotify-profile (phase 6) is updated to enqueue an immediate refresh (call refresh_deck_view() inline if profile_complete_verified changes from false to true; otherwise rely on the scheduled refresh)
      - Integration test: insert a verified user, immediately query deck_view (no row because refresh hasn't run), invoke the refresh Lambda, query again (row appears)
    notes: ""

  - id: 7.5
    title: HTTP API route wiring for match
    agent: backenddeveloper
    done: false
    depends_on: [7.1, 7.2]
    acceptance_criteria:
      - POST /v1/match/search and GET /v1/match/deck are registered as aws_apigatewayv2_route resources with authorizer_id=Cognito JWT and integration pointing at the knotify-match Lambda
      - terraform plan in dev shows the routes added
    notes: ""

  - id: 7.6
    title: End-to-end match ordering test
    agent: backenddeveloper
    done: false
    depends_on: [7.1, 7.2, 7.3, 7.4, 7.5]
    acceptance_criteria:
      - tests/integration/match_e2e_test.py signs up a requester and ten candidate users with deterministic preference vectors (each candidate's vector is the requester's vector plus a known perturbation, producing a strict ranking)
      - POST /v1/match/search returns all ten candidates in the expected pgvector-cosine-distance order; the assertion checks the exact ordering
      - GET /v1/match/deck returns the same set
      - Adding a block of the top-ranked candidate causes that candidate to drop out of both responses
    notes: ""
