# Knotify Backend — Architecture

**Status:** Draft v1.6
**Owner:** Project owner (single developer)
**Last updated:** May 2026
**Intended consumer of this document:** `superpowers:brainstorm` agent for detailed analysis and phase split.

**Changes since v1.5:**

- Owner-resolved open questions during `/create-plan` cross-phase brainstorm:
  - §13 #1 GitHub OIDC vs. static keys → **static keys for v1**, OIDC deferred to pre-launch hardening phase
  - §13 #4 `deck_view` strategy → **materialized view**
  - §13 #5 gender isolation → **PostgreSQL Row-Level Security (RLS)**
  - §13 #7 push provider → **Expo Push Notifications**
  - §13 #8 account deletion → **soft delete** (`deleted_at` timestamp) with 30-day retention then hard purge
  - §13 #21 ChatMessages on user deletion → **anonymize `sender_id`** (preserve text, display as "Deleted User")
  - §13 #18 observability stack → **CloudWatch only** with a **dedicated late-stage observability phase**; per-phase Lambdas ship with Powertools instrumentation from birth (hybrid Option C originally proposed, owner picked Option A — dedicated phase consolidation); **all CloudWatch log groups have a 7-day retention** in both dev and prod to keep storage costs minimal
- Cross-phase sequencing decisions (consumed by `/create-plan` Step 2):
  - Cognito post-confirmation trigger Lambda is **sequenced after** the Aurora schema and Lambda-foundations phases — it is built and unit-tested before Cognito ships, then wired as the final step of the Cognito phase (no half-broken signup state)
  - Chat ships as a **single phase** after the friendships/blocks domain Lambdas exist; the DynamoDB chat data plane and the AppSync GraphQL API plane are not split across phases
- §3.2, §5.1, §5.2, §9.3, §11.1, §11.2 updated inline to reflect the resolutions.

**Changes since v1.4:**

- §10.2 GitHub Actions pipeline rewritten with a complete, valid YAML workflow file (was pseudo-YAML), including explicit `apply-dev`/`apply-prod` jobs gated by branch.
- §10.3 expanded with a worked day-to-day developer flow showing exactly how branch choice → environment routing works (feature branch → development → main → approval).
- §10.4 corrected: secrets are stored as **GitHub Environment secrets** with the same name (`AWS_ACCESS_KEY_ID`) across environments — the environment scope is what picks the right value. Previous draft mistakenly suggested `_DEV`/`_PROD` suffixed repo-level secrets.
- §10.6 smoke-test YAML updated for consistency with the corrected secret-naming approach.

**Changes since v1.3:**

- §4.4 Lambda runtime **resolved**: Python 3.14 on ARM64 (Graviton) for all backend code — business-logic Lambdas, AppSync resolver Lambdas, Step Functions task Lambdas, Cognito triggers, push fan-out. No Node.js anywhere. §13 #2 marked resolved.
- §10.6 added: **mandatory pre-implementation pipeline smoke test** (deploy one S3 bucket through the full Terraform → GitHub Actions → AWS flow in both dev and prod accounts) before any real implementation begins. Includes the Terraform module, GitHub Actions workflow, procedure, failure modes, and exit criteria.

**Changes since v1.2:**

- §5.4 chat data model restructured around **deterministic room IDs** (`sha256(canonical_pair(userA, userB))`), making group chat structurally impossible (per owner requirement). `room_type` field removed; `ChatRooms` now stores `user_a`/`user_b` explicitly for O(1) participant verification.
- §5.4.1 added: full room lifecycle including idempotent creation, deactivation (account deletion + block), and **reactivation on unblock** (per owner preference: preserve message history when same blocker un-blocks).
- §5.4.2 added: explicit end-to-end notification flow with both channels (AppSync subscription + DynamoDB Stream → push fan-out), worked examples for friend request and chat message, and explicit note that chat messages do NOT create rows in the `Notifications` table.
- §8.6, §8.7, §8.8 expanded: deactivation/reactivation security, multi-layer block enforcement, and explicit handling of "deterministic IDs aren't a security issue."
- §9 (Notifications) simplified to point at §5.4.2 instead of duplicating.
- §13 open questions: added #28 (push debouncing).

**Changes since v1.1:**

- §4.2 API surface now derived from the old `KhandaDataAcess` OpenAPI spec, with explicit old→new endpoint migration map and corrections of HTTP-method/naming issues from the old design.
- §5.4 chat data model expanded based on review of old AppSync schema: adopted `ChatRoomMembership` join-table pattern (resolves prior open question), removed `sendToID` redundancy, added `MessageReads` table, added `PushNotificationTokens` table, and called out security risks of Amplify auto-generated subscriptions.
- §5.6 (Notifications: DynamoDB or Aurora?) **resolved**: DynamoDB, confirmed by old schema.
- §8.2 chat security expanded with explicit "explicit scoped subscriptions only" guidance, addressing the Amplify auto-generated `onCreate*`/`onUpdate*` subscription risk.
- §13 open questions updated: items 6, 11, 13 resolved; new items 23–27 added covering ambiguous endpoints from the old API, Cognito post-confirmation trigger pattern, message edit semantics, and `preferred_username` handling.

**Changes since v1:**

- API Gateway revised from REST API to **HTTP API + CloudFront + WAF** (§4.2). ~70% cost reduction at gateway layer + better edge protection.
- Cognito JWT validation now handled by **built-in HTTP API authorizer**; Lambda authorizer demoted to optional/advanced cases (§4.3).
- §11.2 expanded with full Step Functions state machine for account deletion.

---

## 1. Purpose and scope

`knotify-backend` is the backend for a React Native mobile app (iOS + Android) — a social/matchmaking application. It is a full rebuild of a previous DynamoDB-only backend, with the goal of unlocking filter-based and similarity-based matching, while preserving the parts of the old stack that worked well (real-time chat).

This document describes:

- Target architecture
- Component responsibilities and interaction patterns
- Data model (Postgres + DynamoDB)
- Security model (with explicit focus on MITM resistance)
- Deployment topology (dev + prod via Terraform + GitHub Actions)
- Cost model
- Open questions explicitly flagged for the brainstorm agent

It does **not** describe:

- Final REST endpoint definitions (to be provided separately by the owner)
- Mobile app internals
- Implementation phasing (the brainstorm agent will produce this)

---

## 2. High-level architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                         React Native App (iOS / Android)               │
│                       (aws-amplify, AppSync client SDK)                │
└─────────────────────────┬──────────────────────┬───────────────────────┘
                          │                      │
                          │ HTTPS                │ WSS (GraphQL subs)
                          ▼                      ▼
              ┌───────────────────┐   ┌────────────────────────┐
              │  AWS Cognito      │   │  AWS AppSync (GraphQL) │
              │  (User Pool)      │   │  - chat mutations      │
              │  - signup/login   │   │  - chat subscriptions  │
              │  - JWT issuance   │   │  - typing indicators   │
              └────────┬──────────┘   │  - read receipts       │
                       │              └───────┬────────────────┘
                       │ JWT                  │
                       ▼                      │ resolvers
              ┌───────────────────┐           │
              │  CloudFront       │           ▼
              │  + WAF + Shield   │   ┌─────────────────────────┐
              │  (edge DDoS, TLS) │   │ DynamoDB                │
              └────────┬──────────┘   │  - ChatRooms            │
                       │              │  - ChatMessages         │
                       ▼              │  - Notifications        │
              ┌───────────────────┐   │  - PushTokens           │
              │  HTTP API Gateway │   └─────────────────────────┘
              │  + Cognito JWT    │
              │    authorizer     │
              └────────┬──────────┘
                       ▼
        ┌──────────────────────────────────┐
        │  VPC (per environment)            │
        │                                   │
        │   ┌──────────────────────────┐    │
        │   │  Lambda (private subnet) │    │
        │   │  - business logic        │    │
        │   │  - profile CRUD          │    │
        │   │  - matching              │    │
        │   │  - friend ops            │    │
        │   └────────┬─────────────────┘    │
        │            │                       │
        │            ▼                       │
        │   ┌──────────────────────────┐    │
        │   │  Aurora Serverless       │    │
        │   │  (PostgreSQL + pgvector) │    │
        │   │  - profiles              │    │
        │   │  - friendships           │    │
        │   │  - bookmarks             │    │
        │   │  - friend requests       │    │
        │   │  - blocks                │    │
        │   │  - preference vectors    │    │
        │   └──────────────────────────┘    │
        └──────────────────────────────────┘
                       │
                       ▼
                ┌──────────────┐
                │  S3 (later)  │
                │  - photos    │
                └──────────────┘
```

---

## 3. AWS account and environment topology

Two fully isolated environments, one AWS account each, under one AWS Organization.

### 3.1 Decision

- **AWS Organization** with two member accounts:
  - `knotify-dev` (development environment)
  - `knotify-prod` (production environment)
- **No AWS SSO / IAM Identity Center** for now (explicit owner preference).
- **Access**: dedicated IAM users with programmatic access keys per account, used by GitHub Actions OIDC or stored as repository secrets.
- **Root account**: management account only — no workloads.
- **Billing**: consolidated under the management (root) account.

### 3.2 RESOLVED — static keys for v1, OIDC deferred

**Decision:** static IAM access keys for v1. OIDC federation deferred to the pre-launch hardening phase.

OIDC is the safer long-term choice (no static secrets to leak, no rotation needed), but for v1 the owner has chosen static keys to keep the bootstrap surface small. Mitigations applied:

- Rotate quarterly via a manual ritual
- Scope IAM policy to least privilege (Terraform state bucket + Terraform-created resources only)
- Enable CloudTrail in both accounts; alert on root or unusual API activity
- Store keys exclusively as GitHub Environment secrets, never repository secrets (§10.4)

OIDC migration is scheduled as a story in the pre-launch hardening phase; the GitHub Actions workflow (§10.2) is structured so the swap is a credentials change, not a workflow rewrite.

### 3.3 Cross-account concerns

- Terraform state stored in S3 + DynamoDB lock table, **one per account** (not cross-account)
- No resource sharing between dev and prod — full isolation is the goal
- Same Terraform code applied with different `tfvars` files per environment

---

## 4. Component architecture

### 4.1 AWS Cognito (auth)

- **User Pool** as the identity provider
- Issues JWTs (ID token, access token, refresh token) on successful login
- Mobile app uses `aws-amplify` React Native SDK for signup/login/refresh flows
- **Cognito `sub` claim** is the canonical `user_id` used everywhere downstream (Aurora `users.user_id`, AppSync ownership claims, DynamoDB chat partition keys, S3 object prefixes)

**Configuration notes:**

- Email-based signup; phone optional
- Enforce email verification before account is usable
- MFA: optional in v1, required for production accounts (review)
- Password policy: 12+ chars, mixed case, number, symbol
- Token lifetimes: ID/Access = 1 hour, Refresh = 30 days
- Advanced Security Features (compromised credentials, adaptive authentication) — enable in prod

**Out of scope for v1:** social login (Google/Apple) — can be added later via Cognito federated identities.

### 4.2 API Gateway (HTTP API + CloudFront)

**Type decision: HTTP API, fronted by CloudFront.** Revised from initial draft (was REST API).

Reasoning:

- HTTP API is ~70% cheaper than REST API at the gateway layer ($1.00 vs $3.50 per million requests)
- HTTP API has lower request latency and faster cold starts
- The REST-only features we'd give up are not needed here:
  - Request validation at the gateway → Lambda will validate
  - Mapping templates → Lambda handles transformations
  - Built-in caching → data is per-user, not cacheable at gateway
  - Usage plans / API keys → no third-party API consumers
  - Direct WAF integration → solved by putting CloudFront in front (see below)
  - Private endpoints → not needed; API is internet-facing
- HTTP API has a cleaner built-in Cognito JWT authorizer

**Topology:**

```
Mobile App
    ↓ HTTPS (TLS 1.3)
CloudFront distribution (custom domain api.knotify.app, ACM cert)
    ↓ AWS Shield Standard (free DDoS)
    ↓ AWS WAF (rules attached here, not at gateway)
HTTP API (regional)
    ↓ Cognito JWT authorizer (built-in)
    ↓ or Lambda authorizer (only for advanced cases)
Lambda functions (in VPC)
```

**Protection layers (revised):**

1. **CloudFront** absorbs traffic at the edge — DDoS protection via AWS Shield Standard (free), TLS termination, geographic distribution
2. **AWS WAF** attached to CloudFront (not the API itself) with managed rule groups (Core Rule Set, Known Bad Inputs, SQL Injection, Rate-Based rules). Blocking at the edge stops attacks before they reach the API.
3. **HTTP API JWT authorizer** validates Cognito JWT natively — no Lambda required for the common path
4. **Lambda authorizer** used only where built-in JWT auth is insufficient (e.g., per-user rate limiting in DynamoDB, request signing verification if added later)
5. **Per-route throttling** at the stage level (HTTP API supports stage + route throttling)
6. **TLS 1.2 minimum** at CloudFront (TLS 1.3 preferred); HTTPS only
7. **Origin protection**: CloudFront → HTTP API uses a custom header secret so the HTTP API only accepts requests bearing the secret. This prevents bypassing CloudFront/WAF by hitting the HTTP API directly. (CloudFront can inject the secret header automatically; HTTP API rejects requests without it.)
8. **CORS**: locked to known origins; native mobile apps don't send Origin headers, but tightening this matters if a web client is added later

**Cost comparison at 10K users (~30M req/month):**

| Setup                                | Gateway | CloudFront | WAF | Total  |
| ------------------------------------ | ------- | ---------- | --- | ------ |
| REST API + WAF (rejected)            | $105    | —          | $10 | $115   |
| HTTP API + CloudFront + WAF (chosen) | $30     | $5–15      | $10 | $45–55 |

**API surface (derived from the old `KhandaDataAcess` REST API, redesigned for HTTP API conventions):**

The previous backend exposed 14 endpoints, all backed by a single monolithic Lambda (`KhandarData`) that switched on `path` + `http_method` to dispatch internally. The new design uses per-domain Lambdas (§4.4) and consistent REST conventions:

- Resource-style nouns, not verb-prefixed (`/v1/friends/{id}` not `/removefriend`)
- HTTP method indicates action (`DELETE` for remove, `POST` for create — old API used `GET` for state changes, which is broken)
- Versioned base path (`/v1/...`)
- camelCase URL fragments avoided in favor of lowercase-hyphenated
- All routes JWT-authenticated via the built-in Cognito JWT authorizer; `userId` derived from `sub` claim, never accepted as a parameter

#### Migration map

| Old endpoint (method)                                     | New endpoint (method)                                                           | Notes                                                                                                      |
| --------------------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `POST /registeruser`                                      | (handled by Cognito post-confirmation Lambda trigger)                           | Cognito creates the user; trigger inserts initial Aurora row. No REST endpoint needed.                     |
| `GET /userdatabyid`                                       | `GET /v1/profile/me`                                                            | `userId` from JWT, not query param                                                                         |
| `GET /userdata?username=...`                              | `GET /v1/profiles?username=...`                                                 | Username search; rate-limited                                                                              |
| `POST /updateUserData`                                    | `PATCH /v1/profile/me`                                                          | Only mutable fields accepted (§5.7); immutable fields rejected by validator                                |
| `POST /deleteuser`                                        | `DELETE /v1/profile/me`                                                         | Triggers Step Functions workflow (§11)                                                                     |
| `GET /getfriends`                                         | `GET /v1/friends`                                                               | Returns list of friend user IDs + denormalized basic info                                                  |
| `POST /addfriend`                                         | `POST /v1/friend-requests`                                                      | Renamed: this is a _request_, not a direct add. Acceptance is separate.                                    |
| `GET /removefriend` (was GET!)                            | `DELETE /v1/friends/{userId}`                                                   | Old API used GET for a state-changing operation — corrected                                                |
| `GET /handlependingrequests?action=...`                   | `POST /v1/friend-requests/{id}/accept`, `POST /v1/friend-requests/{id}/decline` | Old API overloaded one endpoint with an `action` param; split into two clear endpoints                     |
| `GET /getBookMarkedIds`                                   | `GET /v1/bookmarks`                                                             | Returns bookmark list with denormalized profile info                                                       |
| `POST /bookMarkProfile`                                   | `POST /v1/bookmarks`                                                            | Body: `{ "userId": "..." }`                                                                                |
| `POST /removeBookMarkProfile`                             | `DELETE /v1/bookmarks/{userId}`                                                 | Method change: DELETE is correct                                                                           |
| `POST /getprofileslist`                                   | `POST /v1/match/search`                                                         | Filter-based match query. Now backed by Postgres with hard filters + pgvector ranking, not DynamoDB scans. |
| `POST /getprofileslistbyid`                               | `GET /v1/profiles/{userId}`                                                     | One profile by ID. Returns deck-view fields only (filtered for "other user" view, §5.7)                    |
| `GET /deckdata?filters=...`                               | `GET /v1/match/deck?filters=...`                                                | Swipeable card batch. Now uses Postgres-backed deck view (§5.1)                                            |
| `GET /newsFeed?feedtype=...`                              | `GET /v1/feed?type=...`                                                         | **Open question — purpose unclear from the old API.** Possibly an admin/announcements feed? Flagged §13.   |
| `GET /userverificationdocs?objectKey=...&contentType=...` | `POST /v1/profile/verification-docs`                                            | Phase 2 (S3 photo + document upload). Returns pre-signed S3 URL.                                           |

#### New endpoints (not in the old API)

These are new in v2:

| New endpoint (method)                                 | Purpose                                           |
| ----------------------------------------------------- | ------------------------------------------------- |
| `POST /v1/blocks`                                     | Block a user (body: `{ "userId": "..." }`)        |
| `DELETE /v1/blocks/{userId}`                          | Unblock                                           |
| `GET /v1/blocks`                                      | List blocked users                                |
| `GET /v1/friend-requests`                             | List pending requests (incoming + outgoing)       |
| `DELETE /v1/friend-requests/{id}`                     | Cancel a request you sent (before recipient acts) |
| `GET /v1/profile/me/deletion-status?executionArn=...` | Poll account-deletion progress (§11)              |

#### Endpoints intentionally NOT migrated

- **`POST /registeruser`** — Cognito signup happens client-side via Amplify SDK; a Cognito **post-confirmation Lambda trigger** creates the initial Aurora row. No backend REST call needed during signup.
- **Chat-related endpoints** — chat lives entirely in AppSync (§4.7, §5.4, §8); no REST endpoints for messages.

#### Lambda mapping (domain-grouped, not 1-per-route)

The old design had one Lambda for everything. The new design groups by domain:

| Lambda                              | Routes                                          |
| ----------------------------------- | ----------------------------------------------- |
| `knotify-profile`                   | `/v1/profile/*`, `/v1/profiles*` (read paths)   |
| `knotify-friends`                   | `/v1/friends/*`, `/v1/friend-requests/*`        |
| `knotify-bookmarks`                 | `/v1/bookmarks/*`                               |
| `knotify-blocks`                    | `/v1/blocks/*`                                  |
| `knotify-match`                     | `/v1/match/*`                                   |
| `knotify-deletion-initiator`        | `DELETE /v1/profile/me` → starts Step Functions |
| `knotify-cognito-post-confirmation` | (Cognito trigger, not API Gateway)              |
| `knotify-feed`                      | `/v1/feed` (if kept)                            |
| `knotify-uploads`                   | `/v1/profile/verification-docs` (phase 2)       |

This grouping balances cold-start surface (fewer Lambdas) against deployment isolation (one bad deploy doesn't break everything). Each Lambda has its own IAM role with least-privilege DB and DynamoDB access.

#### Old API design issues, corrected in the new design

For the brainstorm agent: the old API had several issues worth being aware of, all corrected here.

1. **No authorizer.** The old OpenAPI shows no security definition at all. Auth was relied upon entirely client-side via Amplify + IAM on the Lambda. The new design uses a built-in JWT authorizer on every route — no exceptions.
2. **`userId` passed as a query parameter** (e.g., `GET /userdatabyid?userid=...`). This means User A could query User B's data by guessing the UUID, unless the Lambda checked the JWT. The new design **derives `userId` from the JWT `sub` claim** for any "me" operation — never trusts the URL or query string.
3. **GET used for state changes.** `GET /removefriend`, `GET /handlependingrequests` (with `?action=accept/decline`) were state-changing GETs. This is HTTP-incorrect and dangerous: GETs are cacheable, retryable, and can be triggered by image tags or prefetch. Corrected: state changes are POST/PUT/DELETE only.
4. **`action`-parameter overloading.** `/handlependingrequests?action=accept` and `?action=decline` were two endpoints squashed into one. The new design splits them for clarity and auditability.
5. **Naming inconsistency.** `bookMarkProfile`, `addfriend`, `getBookMarkedIds`, `updateUserData` — five different casing/naming conventions. The new design uses consistent kebab-case nouns.
6. **Monolithic Lambda.** A single Lambda dispatched by string-matching the path is hard to test, deploy, and observe. Domain-grouped Lambdas are cleaner.

> **Status**: This is now the working spec, derived from the old API + new requirements. Owner to confirm or amend.

### 4.3 Lambda Authorizer (optional, for advanced cases)

With the move to HTTP API, the **built-in Cognito JWT authorizer** handles routine JWT validation natively — no Lambda required for the standard path. HTTP API natively validates `iss`, `aud`, `exp`, and signature against Cognito's JWKS, and exposes the decoded claims to downstream Lambdas.

A custom **Lambda authorizer** is added only when one of the following is needed:

- Per-user rate limiting (track request counts per `sub` in DynamoDB)
- Verification of HMAC-signed request bodies (if §7.2(c) is adopted)
- Additional claim checks (e.g., "profile must be complete to call this endpoint")
- Anti-replay nonce checking

**Recommendation for v1:** start with built-in JWT authorizer only. Add a Lambda authorizer in v2 if rate limiting or signing becomes a requirement.

If a Lambda authorizer is added later:

- Cache responses by token hash (5 min TTL) to avoid re-verifying on every request
- Use Python `python-jose` or `PyJWT` to verify against JWKS, with JWKS cached in Lambda memory between invocations

### 4.4 Business-logic Lambdas

- One Lambda per logical domain (profile, friends, matching, bookmarks, blocks, account-deletion). Recommendation: **one Lambda per domain** to reduce cold-start surface area while keeping logical separation.
- **Runtime: Python 3.14** — the latest version available on AWS Lambda (added as a managed runtime in November 2025). All business-logic Lambdas, AppSync resolver Lambdas, the push-fan-out Lambda, the Cognito post-confirmation trigger, and the Step Functions step Lambdas use this same runtime. No Node.js anywhere in the backend.
- Architecture: **ARM64 (Graviton)** for ~20% better price-performance over x86_64. No architecture-specific dependencies in the planned libraries (psycopg2-binary, boto3, pgvector client, etc., all have manylinux2014_aarch64 wheels).
- All Lambdas in **private VPC subnets** (see §6).
- Connection pooling: module-level psycopg2 connection reuse for v1; **RDS Proxy** added when traffic justifies it.
- DB credentials: env vars in v1; migrate to **Secrets Manager** before production launch (flagged).
- Dependency packaging: **Lambda layers** for shared deps (psycopg2, boto3 pin, pgvector helpers, JWT verifier); function packages stay small for fast cold starts.
- Recommended libraries: **AWS Lambda Powertools for Python** (logging, tracing, metrics, idempotency) — Anthropic-agnostic AWS-blessed toolkit, supports Python 3.14.

### 4.4.1 Step Functions step Lambdas

All Step Functions tasks (used for account deletion — §11) are also Python 3.14 Lambdas. No Lambdaless / direct service integrations in the state machine, even where they're available (e.g., direct DynamoDB.UpdateItem from a state). Reasoning: keeping every step as a Python Lambda gives one consistent language for unit testing, error handling, and observability, and avoids learning two different "how to write a step" patterns. The performance/cost difference at this volume is negligible.

### 4.5 Aurora Serverless (PostgreSQL)

- Engine: **Aurora PostgreSQL 16 or 17** (latest stable with pgvector 0.8.x)
- Mode: **Aurora Serverless v2** (recently renamed "Aurora Serverless")
- Capacity: min 0 ACU (auto-pause) in dev, min 0.5 ACU in prod; max 4 ACU dev, max 8 ACU prod (adjustable)
- Storage: **Aurora Standard** (not I/O-Optimized) for testing phase
- Placement: **private subnets only**, `PubliclyAccessible = false`
- Backups: 7 days retention (dev), 30 days (prod)
- Extensions enabled: `vector`, `pg_trgm` (fuzzy text search), `pgcrypto` (UUIDs)

### 4.6 DynamoDB

- Used **only** for high-write, time-ordered, key-access workloads:
  - **ChatRooms**
  - **ChatMessages**
  - **PushNotificationTokens** (likely)
  - Possibly **Notifications inbox** (open question — see §5.6)
- Billing mode: on-demand (pay-per-request) for both environments
- Point-in-time recovery: enabled in prod
- Streams: enabled on ChatMessages for fan-out to notifications

### 4.7 AWS AppSync (GraphQL — chat)

- One AppSync API per environment
- Auth mode: **Cognito User Pool** (primary), with IAM as secondary (for backend service access)
- Resolvers:
  - **Direct DynamoDB resolvers** where possible (lower latency, no Lambda cost)
  - **Lambda resolvers** for anything requiring authorization checks beyond basic ownership (e.g., "is this user a participant of this chatroom?")
- Subscriptions over WSS (TLS)
- See §8 for full chat security model

### 4.8 S3 (deferred to phase 2)

- Profile photos
- Bucket per environment
- Block all public access; serve via CloudFront with signed URLs OR Lambda-signed pre-signed URLs
- Server-side encryption (SSE-S3 or SSE-KMS)
- Lifecycle rule: delete orphaned uploads after 24h

### 4.9 AWS Amplify

- Used **only as a client SDK** on React Native for Cognito auth flows and AppSync subscriptions
- **No Amplify hosting / Amplify backend (Gen 2) used** — Terraform owns the infrastructure
- This is an important distinction: Amplify here is a library, not a deployment system

---

## 5. Data model

### 5.1 Aurora PostgreSQL — schema (draft v1)

#### `users`

Primary profile data. Field types are corrected from the old DynamoDB schema (proper types, not all strings).

```sql
CREATE TABLE users (
    user_id              UUID PRIMARY KEY,                  -- Cognito sub
    email                TEXT UNIQUE NOT NULL,
    phone_number         TEXT,
    username             TEXT NOT NULL,

    -- IMMUTABLE fields (set at signup, never updated after profile completion)
    first_name           TEXT NOT NULL,
    last_name            TEXT NOT NULL,
    sex                  TEXT NOT NULL CHECK (sex IN ('Male','Female')),
    birthday             DATE NOT NULL,
    religion             TEXT,
    subsect              TEXT,

    -- MUTABLE fields
    age                  INTEGER GENERATED ALWAYS AS (DATE_PART('year', AGE(birthday)))::int STORED,
    chosen_profile_avatar TEXT,
    photo_url            TEXT,
    college_name         TEXT,
    current_residence_city    TEXT,
    current_residence_country TEXT,
    resident_country_code     CHAR(2),
    district             TEXT,
    education_level      TEXT,
    employer_name        TEXT,
    employment_type      TEXT,
    family_residence_address TEXT,
    father_retired       TEXT,
    fathers_job          TEXT,
    fathers_name         TEXT,
    graduation_year      INTEGER,
    has_children         BOOLEAN,
    higher_secondary     TEXT,
    higher_secondary_passing_year INTEGER,
    highest_degree       TEXT,
    high_school          TEXT,
    high_school_passing_year INTEGER,
    job_title            TEXT,
    marital_status       TEXT,
    marriage_time        TEXT,
    mother_retired       TEXT,
    mothers_job          TEXT,
    mothers_name         TEXT,
    move_abroad          BOOLEAN,
    office_address       TEXT,
    partners_religious_level TEXT,
    professional_category TEXT,
    profile_complete_verified BOOLEAN DEFAULT false,
    relation             TEXT,
    religious_level      TEXT,
    salary_range         TEXT,

    -- structured blob, JSONB so individual prefs can be queried with GIN
    preferences          JSONB DEFAULT '{}'::jsonb,

    -- vector for similarity ranking (20 boolean prefs => 20-D vector)
    preference_vector    vector(20),

    -- audit
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at           TIMESTAMPTZ,                       -- soft-delete (see §11)

    CONSTRAINT email_format CHECK (email ~* '^[^@]+@[^@]+\.[^@]+$')
);

-- Indexes
CREATE INDEX idx_users_sex_country_religion ON users (sex, current_residence_country, religion) WHERE deleted_at IS NULL;
CREATE INDEX idx_users_age ON users (age) WHERE deleted_at IS NULL;
CREATE INDEX idx_users_prefs_gin ON users USING GIN (preferences);
CREATE INDEX idx_users_vector ON users USING hnsw (preference_vector vector_cosine_ops);
```

#### `siblings` (1-to-many, replaces embedded list)

```sql
CREATE TABLE siblings (
    sibling_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name             TEXT,
    gender           TEXT,
    sibling_age      TEXT,
    marital_status   TEXT,
    profession       TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_siblings_user ON siblings (user_id);
```

#### `friendships` (bidirectional, deduplicated)

Stores one row per friendship pair, canonicalized so the smaller UUID is always `user_a`.

```sql
CREATE TABLE friendships (
    user_a       UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    user_b       UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_a, user_b),
    CHECK (user_a < user_b)
);
CREATE INDEX idx_friendships_b ON friendships (user_b);
```

To find all friends of a user: `SELECT user_b FROM friendships WHERE user_a = :id UNION SELECT user_a FROM friendships WHERE user_b = :id`.

#### `friend_requests`

```sql
CREATE TABLE friend_requests (
    request_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_user_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    to_user_id     UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    status         TEXT NOT NULL CHECK (status IN ('pending','accepted','declined','cancelled')),
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    responded_at   TIMESTAMPTZ,
    UNIQUE (from_user_id, to_user_id, status) -- prevents duplicate pending requests
);
CREATE INDEX idx_fr_to_pending ON friend_requests (to_user_id) WHERE status = 'pending';
CREATE INDEX idx_fr_from_pending ON friend_requests (from_user_id) WHERE status = 'pending';
```

#### `bookmarks`

```sql
CREATE TABLE bookmarks (
    user_id              UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    bookmarked_user_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, bookmarked_user_id)
);
CREATE INDEX idx_bookmarks_target ON bookmarks (bookmarked_user_id);
```

#### `blocks`

```sql
CREATE TABLE blocks (
    blocker_id    UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    blocked_id    UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (blocker_id, blocked_id)
);
CREATE INDEX idx_blocks_blocked ON blocks (blocked_id);
```

Blocks are unidirectional in storage but enforced bidirectionally in queries: if A blocks B, neither sees the other in match results, search, or chat.

#### `deck_view` (materialized view, replaces old `deckdata` table)

Subset of profile fields for the swipe deck. Auto-refreshed.

```sql
CREATE MATERIALIZED VIEW deck_view AS
SELECT user_id, email, age, chosen_profile_avatar, current_residence_city,
       current_residence_country, first_name, job_title, last_name, photo_url,
       profile_complete_verified, resident_country_code, sex, username
FROM users
WHERE deleted_at IS NULL AND profile_complete_verified = true;

CREATE UNIQUE INDEX idx_deck_user ON deck_view (user_id);
```

Refreshed on profile update via trigger or periodic job. **RESOLVED:** materialized view chosen (per owner decision) — trades a refresh-on-write cost for fast deck-read latency, which matches the access pattern (deck is read constantly; profile mutations are rare). The refresh strategy (per-row trigger vs. scheduled `REFRESH MATERIALIZED VIEW CONCURRENTLY`) is decided inside the Aurora schema phase.

### 5.2 Gender-isolation: separate tables vs. row-level filtering

The owner asked: should male and female users live in separate tables or databases?

**Recommendation: single `users` table with strict query-level enforcement.**

Reasoning:

- Separate tables means duplicated schema, duplicated indexes, duplicated migrations, and a `friendships` table that has to reference both. The friendship case is the killer — you can't have a single FK constraint across two tables.
- Strict enforcement is achievable at three layers:
  1. **Application layer**: every query that returns "other users" includes `WHERE sex = <opposite_of_requesting_user>` as a non-negotiable clause
  2. **Database layer**: PostgreSQL **Row-Level Security (RLS)** policies that enforce this even if application code forgets
  3. **Code review / lint rule**: any direct query against `users` without an RLS-aware session context is rejected

Example RLS policy:

```sql
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

CREATE POLICY users_opposite_sex_only ON users
  FOR SELECT
  USING (
    sex != current_setting('app.requesting_user_sex', true)
    OR user_id = current_setting('app.requesting_user_id', true)::uuid
  );
```

Lambda sets these session variables after authorizing the request:

```python
cur.execute("SET LOCAL app.requesting_user_id = %s", (user_id,))
cur.execute("SET LOCAL app.requesting_user_sex = %s", (user_sex,))
```

This is **defense in depth** — even a SQL injection that bypassed `WHERE` clauses would still be filtered by RLS.

**RESOLVED:** RLS approach adopted (per owner decision). Single `users` table with PostgreSQL Row-Level Security enforcing opposite-sex visibility, set per request via the `app.requesting_user_sex` session GUC. Application-layer `WHERE sex = ?` clauses remain in every query as defense in depth — RLS is the backstop, not the only fence.

### 5.3 What goes in DynamoDB vs. Aurora

| Data                                       | Store               | Rationale                                      |
| ------------------------------------------ | ------------------- | ---------------------------------------------- |
| User profile                               | Aurora              | Relational, queryable, joined often            |
| Friendships                                | Aurora              | Relational by definition                       |
| Friend requests                            | Aurora              | Small, queryable, joined with users            |
| Bookmarks                                  | Aurora              | Small, joined with users                       |
| Blocks                                     | Aurora              | Critical for filtering, joined with users      |
| Preferences vector                         | Aurora (pgvector)   | Co-located with profile for fast join + filter |
| **Chat rooms**                             | **DynamoDB**        | Key access by room ID; high write volume       |
| **Chat messages**                          | **DynamoDB**        | Time-ordered, high write, partition by room    |
| **Push notification tokens**               | **DynamoDB**        | Key access by user ID, frequent overwrites     |
| Notifications inbox (in-app)               | **OPEN — see §5.6** | Could go either way                            |
| Audit log (account deletion, blocks, etc.) | DynamoDB or Aurora  | Append-only, low query; DynamoDB cheaper       |

### 5.4 Chat data model (DynamoDB)

#### Reference: what the old schema had

The old AppSync schema (auto-generated by Amplify Gen 1) defined the chat domain as four DynamoDB-backed types:

```graphql
type User # id, name, email, imageUri, status
type ChatRoom # id, lastMessageID, messages (1-to-many)
type ChatRoomUser # join table: (id, userID, chatRoomID)
type Message # id, content, userID (sender), sendToID (recipient), chatRoomID
type Notification # id, notificationType, senderUserID, sendToID, unread, received
```

Observations from the old schema:

- **`ChatRoomUser` as an explicit join table** between `User` and `ChatRoom`. Supports group chat semantics (N users per room). The new design **does not want group chat**, so this pattern is restructured (see deterministic room ID below).
- **`Message.sendToID` field** alongside `chatRoomID`. Redundant in a strict 1-to-1 room model.
- **`Notification` type was in AppSync/DynamoDB**, not in REST. Confirms the §5.6 decision: notifications stay in DynamoDB with real-time AppSync subscriptions.
- **`User` was duplicated in DynamoDB** — denormalized subset of the profile. Eliminated in the new design.
- **Auto-generated CRUD subscriptions** — security risk. Replaced by explicit scoped subscriptions (§8.2).

#### Design principle: 1-to-1 chat enforced structurally

**Hard requirement: every chat room has exactly 2 participants, fixed for the lifetime of the room. Group chat is structurally impossible, not just disabled by a flag.**

The mechanism: **deterministic room IDs derived from the two user IDs.** The room ID is a function of the participants, not a random UUID. This makes it physically impossible to represent a third participant — there is no room ID that encodes 3 users.

```
room_id = sha256(canonical_pair(userA_id, userB_id))

where canonical_pair(a, b) = min(a, b) + ":" + max(a, b)
```

Sorting the two UUIDs before hashing ensures `room_id(A, B) == room_id(B, A)` — the order doesn't matter, so there's one canonical room per pair of users.

Consequences:

| Property                          | Result                                                                                           |
| --------------------------------- | ------------------------------------------------------------------------------------------------ |
| Number of members per room        | Exactly 2, by construction                                                                       |
| Number of rooms between two users | Exactly 0 or 1, ever                                                                             |
| Adding a 3rd participant          | Impossible — no valid room ID encodes 3 users                                                    |
| "Find my room with Alice"         | Compute `sha256(canonical_pair(me, alice))` — no query needed                                    |
| "Create room with Alice"          | Idempotent — same computation every time                                                         |
| Room ID predictability            | Not a security issue: knowing a room ID doesn't grant access (membership is enforced separately) |

This removes the `room_type` field entirely — there's no other type. It also removes the "what if a Lambda accidentally adds a 3rd member" failure mode.

#### `ChatRooms` table

```
PK: room_id  (deterministic: sha256(canonical_pair(userA, userB)))
SK: (none — single item per room)
Attributes:
  user_a                   : the lexicographically smaller user UUID
  user_b                   : the lexicographically larger user UUID
  created_at               : ISO timestamp of first creation
  last_message_id          : pagination cursor for messages
  last_message_at          : ISO timestamp (drives room sorting in client)
  last_message_preview     : first ~80 chars of last message (UI room-list)
  last_message_sender_id   : who sent the last message (for "You: ..." prefix)
  status                   : 'active' | 'deactivated'
  deactivated_reason       : 'user_deleted_account' | 'blocked' | null
  deactivated_at           : ISO timestamp, set when transitioning to 'deactivated'
  deactivated_by           : user_id who triggered deactivation (for block scenarios)
  reactivated_at           : ISO timestamp, set on reactivation (see §5.4.1)
```

Storing both `user_a` and `user_b` explicitly (not just relying on the hash) lets resolvers verify "is the requesting user one of these two?" in O(1) without recomputing the hash.

#### `ChatRoomMembership` table (per-user room state)

The room item holds shared state. Per-user state (read pointers, mute, cached display info) lives separately, keyed by `(user_id, room_id)`. By construction, exactly 2 rows exist per room.

```
PK: user_id
SK: room_id
Attributes:
  other_user_id          : the other participant's user_id (denormalized)
  joined_at              : ISO timestamp
  last_read_message_id   : ID of last message this user read
  last_read_at           : timestamp
  cached_other_name      : other user's display name (refreshed via Aurora trigger)
  cached_other_avatar    : other user's avatar URL (refreshed via Aurora trigger)
  notifications_muted    : boolean (per-user, per-room mute flag)
  hidden                 : boolean (user "deleted" the conversation from their list — but other party still sees it)
```

No GSI needed for "list participants of room" — given a `room_id`, the participants are derivable from `ChatRooms.user_a` and `ChatRooms.user_b` directly. The membership table is queried only by `user_id` to list a user's rooms, sorted client-side or via secondary index on `last_read_at` if needed.

**Operations:**

- **List my rooms**: query base table with `PK = my_user_id`. Returns all rooms with denormalized other-party info — single query, no joins.
- **Get my state for a specific room**: `GetItem(user_id, room_id)` — single point read.
- **Authorize a subscription/mutation**: `GetItem(user_id, room_id)` — if it exists, the user is a participant.

#### `ChatMessages` table

```
PK: room_id
SK: created_at#message_id   (ISO timestamp + monotonic suffix, sortable)
Attributes:
  sender_id       : user_id of sender (server-set from JWT, never client-supplied)
  content         : message text (UTF-8, supports emojis, multi-language)
  content_type    : 'text' | 'gif'
  gif_url         : optional, only when content_type = 'gif' (URL whitelisted)
  delivered_at    : ISO timestamp, server-set on insert
  ttl             : optional, for retention policy
```

**Differences from the old schema:**

- No `sendToID` — recipient is implicit (room ID → 2 participants → not-me = recipient)
- `content_type` field — explicit text vs. GIF
- `delivered_at` server-set, not client-set

#### `MessageReads` table

Per-user read receipts, kept separate from `ChatMessages` to avoid hot-partition writes when both parties are actively reading a busy room.

```
PK: room_id
SK: user_id
Attributes:
  last_read_message_id  : ID of last message read by this user in this room
  last_read_at          : timestamp
```

Source of truth for read receipts. `ChatRoomMembership.last_read_message_id` is a cached duplicate updated in the same DynamoDB transaction, used for fast room-list rendering without an extra read.

#### `Notifications` table

In-app notification inbox. Stored in DynamoDB, read via AppSync subscriptions for real-time delivery.

```
PK: user_id
SK: created_at#notification_id   (sortable)
Attributes:
  type            : 'friend_request' | 'friend_request_accepted'
                   | 'profile_view' | 'room_deactivated' | 'bookmark_received'
  payload         : JSON, notification-type-specific
  sender_user_id  : optional, for user-originated notifications
  sender_name     : denormalized display name (for "Alice sent you...")
  sender_avatar   : denormalized avatar URL
  read            : boolean (default false)
  delivered       : boolean (push delivery success flag, set by push fan-out Lambda)
  ttl             : 90 days from creation
GSI1 (UnreadIndex):
  PK: user_id
  SK: notification_id (sparse — only items where read = false)
```

**Important: chat messages do NOT create rows in this table.** A new `ChatMessages` row IS the notification — the recipient's `onMessageInRoom` subscription delivers it in real time, and the push fan-out Lambda fires from the `ChatMessages` DynamoDB Stream. Putting chat messages in both `ChatMessages` and `Notifications` would be redundant.

`Notifications` is reserved for non-chat events (friend requests, profile views, system messages, room deactivation alerts).

#### `PushNotificationTokens` table

```
PK: user_id
SK: device_id
Attributes:
  push_token       : Expo/SNS token
  platform         : 'ios' | 'android'
  last_seen        : timestamp
  app_version      : string
```

Updated each time the mobile app starts and has a fresh token. Stale tokens (last_seen > 60 days) cleaned up by scheduled Lambda.

#### `TypingIndicators` — ephemeral, no storage

Typing is pub/sub only. AppSync `None` data source: the mutation `setTyping(roomId, isTyping)` validates membership, fans out to the subscription `onTypingInRoom(roomId)`, and stores nothing.

---

### 5.4.1 Room lifecycle: creation, deactivation, reactivation

The deterministic room ID has clean implications for the room lifecycle.

#### Creating a room (idempotent)

Triggered when a user opens chat with another user for the first time (typically: from a friend's profile, "Send message" button).

```
1. Client → AppSync mutation: createOrGetRoom(otherUserId)
2. Resolver:
   a. Verify caller (sub) and otherUserId are not the same user
   b. Verify caller and otherUserId are friends (query Aurora friendships)
   c. Verify neither has blocked the other (query Aurora blocks)
   d. Compute room_id = sha256(canonical_pair(sub, otherUserId))
   e. ChatRooms: PutItem with condition attribute_not_exists(room_id)
       - If condition fails (room exists): proceed to step f without writing
       - If condition succeeds: room is new; create both ChatRoomMembership rows
   f. Return room_id and room metadata
```

**Idempotent by design.** Calling `createOrGetRoom(alice)` twice always returns the same `room_id`, regardless of whether the room existed before. No duplicate rooms possible.

#### Deactivating a room

Triggered by either:

- One party deletes their account (Step Functions deletion workflow, §11)
- One party blocks the other

```
ChatRooms: UpdateItem
  status = 'deactivated'
  deactivated_reason = 'user_deleted_account' | 'blocked'
  deactivated_at = NOW()
  deactivated_by = user_id of the acting party (for block case)
```

While `status = 'deactivated'`:

- `sendMessage` mutation: rejected by resolver (room status check)
- `onMessageInRoom` subscription: still allowed (so the remaining user can see history but receives no new messages)
- AppSync publishes `roomDeactivated` event to the other party once

#### Reactivating a room (the unblock case)

If User A blocked User B, then later unblocks B, the room reactivates with full history preserved.

```
On unblock(blocked = B):
1. Aurora: DELETE FROM blocks WHERE blocker_id = A AND blocked_id = B
2. Compute room_id = sha256(canonical_pair(A, B))
3. ChatRooms: UpdateItem with conditional check
   - Only update if status = 'deactivated' AND deactivated_reason = 'blocked'
     AND deactivated_by = A   (A is the one un-blocking)
   - Set status = 'active', reactivated_at = NOW()
   - Clear deactivated_reason, deactivated_at, deactivated_by
4. AppSync publish: roomReactivated event to both parties
5. Both clients refresh the room; existing messages are visible; new messages allowed
```

**Important conditions for reactivation:**

| Scenario                                         | Reactivates?              | Why                                                                               |
| ------------------------------------------------ | ------------------------- | --------------------------------------------------------------------------------- |
| A blocked B, A unblocks B                        | Yes                       | Same actor unblocking                                                             |
| A blocked B, B tries to message                  | No                        | Block is one-directional, B can't undo A's block                                  |
| A blocked B AND B blocked A, A unblocks          | No                        | Still blocked from B's side; need both blocks removed                             |
| Room was deactivated by account deletion         | No                        | The other party no longer exists; reactivation would be meaningless               |
| Both parties block each other, then both unblock | Yes, after second unblock | Last unblock triggers the reactivation update; the conditional check ensures this |

The conditional `UpdateItem` in DynamoDB makes the "two-block, two-unblock" case safe: each unblock attempts to reactivate, but only the unblock that clears the last remaining block actually flips the status. The first unblock's conditional update fails silently (the other party's block is still in place).

**Message history is preserved.** When a room reactivates, all messages from before deactivation are visible to both users. This is intentional per the owner's preference. The client UI may want to render a divider: "── Conversation resumed ──" using `reactivated_at` as the boundary.

#### Account deletion vs. block — irreversibility

Deactivation due to account deletion is **permanent**. The user no longer exists, so reactivation has no meaning. The conditional check on `deactivated_reason = 'blocked'` (above) prevents accidental reactivation in this case.

If the deleted user later re-registers with a new account (different Cognito sub, different UUID), the new account has a different `user_id`, and the deterministic `room_id` between them and any other user will be different. A fresh room. Old history stays with the old (deactivated) room and is orphaned.

---

#### Summary of changes from old schema

| Old (Amplify auto-gen)                                                  | New                                                                                       | Why                                                |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `User` type in DynamoDB (denormalized profile)                          | Eliminated                                                                                | Aurora is single source of truth                   |
| `ChatRoom.id` random UUID                                               | `room_id = sha256(canonical_pair(userA, userB))`                                          | Structurally enforces 1-to-1 + idempotent creation |
| `ChatRoom.room_type`                                                    | Removed entirely                                                                          | No other type exists; field was lying              |
| `ChatRoomUser` (free-form join, N members possible)                     | `ChatRoomMembership` (exactly 2 rows per room, enforced by room ID)                       | Group chat impossible by construction              |
| `ChatRoom.lastMessageID` only                                           | `last_message_id` + `last_message_at` + `last_message_preview` + `last_message_sender_id` | Fast room-list rendering without N+1 reads         |
| `Message.sendToID`                                                      | Removed                                                                                   | Recipient implicit from `room_id`                  |
| `Message.createdAt: String` (client-set)                                | `delivered_at` server-set                                                                 | Anti-spoofing                                      |
| `Notification` with auto CRUD subscriptions                             | Server-only writes, scoped subscriptions, no chat-message rows                            | Security + redundancy elimination                  |
| No `MessageReads` table                                                 | Added                                                                                     | Scale-friendly read receipts                       |
| No `PushNotificationTokens`                                             | Added                                                                                     | For push fan-out                                   |
| Auto-generated `onCreate/onUpdate/onDelete` subscriptions for all types | Explicit scoped subscriptions only                                                        | Security                                           |
| Room creation not idempotent (random UUID)                              | Idempotent via deterministic ID                                                           | Eliminates duplicate-room class of bugs            |
| Permanent deactivation (no reactivation path)                           | Reactivation supported for unblock case                                                   | Owner preference: preserve history on unblock      |

---

### 5.4.2 Notification flow (end-to-end)

Two channels deliver every user-facing event: real-time via AppSync subscription, and push via DynamoDB Stream → push fan-out Lambda. The flow is the same for both chat messages and non-chat notifications; only the source table differs.

#### Channel A — In-app real-time (WebSocket via AppSync)

For users with the app open. The mobile app maintains long-lived AppSync subscriptions on app start.

```
Active subscriptions per user (subset):
  onNotificationForMe                 → new rows in Notifications where user_id = me
  onMessageInRoom(roomId: X)          → new rows in ChatMessages for each room I'm in
  onTypingInRoom(roomId: X)           → pub/sub, no storage
  onRoomDeactivated(roomId: X)        → room status changes
  onFriendRequestUpdated              → friend request state changes (sent/accepted/declined)
```

When the originating Lambda writes the row, AppSync's subscription filters automatically detect the matching row and push it over the open WebSocket — no extra fan-out logic needed.

#### Channel B — Push notification (Apple/Google via DynamoDB Streams)

For users with the app closed or backgrounded. Independent of WebSocket connectivity.

```
DynamoDB Stream on Notifications and ChatMessages
   ↓ (INSERT events only)
PushFanout Lambda triggered
   ↓
For each new item:
  1. Determine recipient(s):
     - Notifications: recipient = item.user_id
     - ChatMessages: query ChatRooms[room_id], recipient = whichever of {user_a, user_b} is NOT sender_id
  2. Look up recipient's tokens: query PushNotificationTokens where user_id = recipient
  3. Check do-not-disturb: query ChatRoomMembership for notifications_muted (chat case)
  4. Compose push payload (title, body, deep-link)
  5. Call Expo Push API (or SNS) with each token
  6. Update Notifications row: set delivered = true on success (chat messages don't track delivery)
  7. On Expo "DeviceNotRegistered" error: delete the stale token from PushNotificationTokens
```

The Lambda is idempotent: re-processing the same stream record is safe (push APIs deduplicate via their own message IDs; setting `delivered = true` twice is a no-op).

#### Worked example: Bob sends a friend request to Alice

```
Step 1. Bob taps "Send friend request" in the app.

Step 2. POST /v1/friend-requests {"toUserId": "alice-uuid"}
        - JWT validated by HTTP API Cognito authorizer (Bob's sub extracted)
        - Routed to knotify-friends Lambda

Step 3. Friends Lambda:
        - Verifies Bob and Alice are not the same user
        - Verifies no existing accepted friendship
        - Verifies no existing pending request
        - Verifies neither has blocked the other
        - INSERT INTO friend_requests (from_user_id, to_user_id, status='pending')
        - PutItem into Notifications:
            user_id: alice-uuid (recipient)
            type: 'friend_request'
            sender_user_id: bob-uuid
            sender_name: 'Bob'
            sender_avatar: 'https://...'
            payload: {request_id: '...'}
            read: false
            delivered: false
        - Returns 201 to Bob's app

Step 4. AppSync subscription fires (Channel A):
        - Alice has subscription onNotificationForMe active (app open)
        - The Notifications.user_id = alice matches her filter
        - AppSync pushes the notification over WebSocket
        - Alice's bell icon updates instantly; toast appears

Step 5. DynamoDB Stream fires (Channel B):
        - PushFanout Lambda triggered with the new Notifications row
        - Looks up alice's push tokens
        - Calls Expo Push: title="Bob sent you a friend request", deep-link to request screen
        - Apple/Google deliver to Alice's lock screen (if app is closed)
        - Updates Notifications row: delivered = true
```

If Alice's app is open, she sees the in-app update from Step 4 and the OS push from Step 5 may or may not appear (the OS suppresses push for foreground apps by default, which is correct behavior).

#### Worked example: Bob sends a chat message to Alice

```
Step 1. Bob types "hello" in their existing chat room with Alice.

Step 2. App calls AppSync mutation:
        sendMessage(roomId: <hash(bob,alice)>, content: "hello", contentType: TEXT)
        - JWT validated by AppSync's Cognito auth mode

Step 3. sendMessage resolver (Lambda):
        - Verifies Bob is a member: GetItem ChatRoomMembership(bob, room_id)
        - Verifies room status is 'active': GetItem ChatRooms(room_id)
        - Inserts into ChatMessages:
            room_id, created_at#message_id, sender_id=bob, content='hello'
            delivered_at = server NOW()
        - Updates ChatRooms: last_message_id, last_message_at,
            last_message_preview, last_message_sender_id=bob
        - Returns Message object to Bob

Step 4. AppSync subscription fires (Channel A):
        - Alice has subscription onMessageInRoom(roomId: <hash(bob,alice)>) active
        - AppSync pushes the new Message
        - Alice's chat screen updates instantly

Step 5. DynamoDB Stream on ChatMessages fires (Channel B):
        - PushFanout Lambda triggered with the new ChatMessages row
        - Determines recipient: ChatRooms.user_b (or user_a, whichever != sender)
        - Looks up Alice's push tokens
        - Checks ChatRoomMembership(alice, room_id).notifications_muted
        - If not muted: sends push "Bob: hello" with deep-link to room
        - If muted: skips push (but Channel A subscription still works if app is open)

Note: NO row is inserted into the Notifications table for chat messages.
      The ChatMessages row IS the notification source for both channels.
```

#### What if Alice's app is offline at the time?

- **Channel A** (WebSocket): no active subscription, message not pushed in real time.
- **Channel B** (Push): still works; OS delivers banner.
- **App reopens**: Amplify's GraphQL client fetches missed messages via `messagesByChatRoom(roomId, createdAt: {gt: lastSeenTimestamp})` query. Catch-up is the client's responsibility.

#### What if Bob spams 50 messages?

- 50 `ChatMessages` rows inserted → 50 stream events → 50 push fan-out invocations.
- Expo Push handles deduplication at their layer (they collapse rapid notifications on the device).
- Optionally: PushFanout Lambda can debounce by checking "was a push for this room sent in the last 5 seconds?" using a small DynamoDB counter. **Flagged in §13 as a v2 optimization.**

#### Notifications table is never written by the client

- All `Notifications` rows are written by **backend Lambdas in response to authenticated actions** (a friend request was sent → write notification for recipient).
- The mobile app can **read** notifications (via AppSync query or subscription) and **mark them as read** (via `markNotificationAsRead` mutation), but cannot **create** them.
- This eliminates an entire class of abuse vectors (one user spamming fake notifications to another).

### 5.5 Preference vector encoding

The `preferences` JSONB has ~20 booleans. Map to a 20-D vector at write time:

```python
PREFERENCE_KEYS = [
  'highlyeducated', 'moderateeducated', 'basiceducated',
  'familyoriented', 'homeoriented', 'workoriented', 'religionoriented',
  'talkative', 'reserved', 'cheerful', 'serious', 'listener',
  'intelligent', 'welldressed', 'athletic',
  'travel', 'cooking', 'reading', 'movies', 'nature',
]

def encode_prefs(prefs: dict) -> list[float]:
    return [1.0 if prefs.get(k, False) else 0.0 for k in PREFERENCE_KEYS]
```

For matching, the workflow is:

1. **Hard filters** via SQL (`sex`, `religion`, `country`, `age` range, mutual blocks)
2. **Soft ranking** via `preference_vector <=> :user_vector` (cosine distance)
3. Return top N

```sql
SELECT u.user_id, u.username, u.age, ...,
       (1 - (u.preference_vector <=> $1)) AS similarity_score
FROM users u
WHERE u.deleted_at IS NULL
  AND u.profile_complete_verified = true
  AND u.sex = $2                            -- opposite sex
  AND u.religion = $3
  AND u.current_residence_country = ANY($4)
  AND u.age BETWEEN $5 AND $6
  AND NOT EXISTS (
    SELECT 1 FROM blocks b
    WHERE (b.blocker_id = u.user_id AND b.blocked_id = $7)
       OR (b.blocker_id = $7 AND b.blocked_id = u.user_id)
  )
ORDER BY u.preference_vector <=> $1
LIMIT 50;
```

### 5.6 RESOLVED — Notifications: DynamoDB

**Decision: DynamoDB.** Confirmed by review of old AppSync schema (§5.4), which used DynamoDB for `Notification` types. Full schema is in §5.4 above (`Notifications` table). This question is closed.

### 5.7 Immutable vs. mutable profile fields

| Field                                | Mutable?                      | Notes                           |
| ------------------------------------ | ----------------------------- | ------------------------------- |
| `user_id`, `email`                   | No                            | Cognito-managed identity        |
| `first_name`, `last_name`            | No                            | Anti-catfishing                 |
| `sex`                                | No                            | Hard partition key for matching |
| `birthday`                           | No                            | Identity-critical               |
| `religion`, `subsect`                | No                            | Matching-critical               |
| `phone_number`                       | Yes, with re-verification     | Cognito flow                    |
| `username`                           | Yes, rate-limited (1/30 days) | Public display                  |
| Job, education, residence, family    | Yes                           | Life changes                    |
| `preferences`                        | Yes                           | Core matching input             |
| `photo_url`, `chosen_profile_avatar` | Yes                           | Subject to moderation           |

Enforced at:

- **API Gateway** request schema (only mutable fields accepted on PATCH)
- **Lambda** validation
- **Database trigger** that raises on any UPDATE touching immutable columns (defense in depth)

```sql
CREATE OR REPLACE FUNCTION enforce_immutable_fields()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.first_name IS DISTINCT FROM OLD.first_name
       OR NEW.last_name IS DISTINCT FROM OLD.last_name
       OR NEW.sex IS DISTINCT FROM OLD.sex
       OR NEW.birthday IS DISTINCT FROM OLD.birthday
       OR NEW.religion IS DISTINCT FROM OLD.religion
       OR NEW.subsect IS DISTINCT FROM OLD.subsect THEN
        RAISE EXCEPTION 'Attempted to modify immutable field';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_immutable
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION enforce_immutable_fields();
```

---

## 6. Networking and VPC

### 6.1 VPC layout (per environment)

```
VPC (10.0.0.0/16)
├── Public subnets    10.0.1.0/24, 10.0.2.0/24  (2 AZs, for future NAT/ALB)
├── Private subnets   10.0.11.0/24, 10.0.12.0/24 (2 AZs, Lambda + Aurora)
└── DB subnets        10.0.21.0/24, 10.0.22.0/24 (2 AZs, Aurora subnet group)
```

### 6.2 Lambda ↔ Aurora connection

The owner's preference is the right one: **Lambda inside the VPC, connecting to Aurora over the private network. No public endpoint, no internet, no VPC endpoint needed (since Aurora is in the same VPC).**

The choices were:

| Option                                                                  | Verdict                                                                                                                                                                                                   |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lambda outside VPC → Aurora public endpoint over internet               | **Rejected.** Worst security.                                                                                                                                                                             |
| Lambda outside VPC → Aurora via RDS Data API (HTTPS)                    | Viable but per-request HTTP overhead and per-request cost. Considered for ultra-low-traffic phase, but rejected for v1 because matching queries return many rows and benefit from persistent connections. |
| **Lambda inside VPC private subnet → Aurora in same VPC over TCP/5432** | **CHOSEN.** Private, fast, free of NAT/endpoint costs.                                                                                                                                                    |
| Lambda inside VPC → Aurora via VPC Endpoint                             | N/A — VPC endpoints are for cross-VPC or service-to-VPC traffic. Aurora is _in_ the VPC, so no endpoint is needed.                                                                                        |

### 6.3 Security groups

| SG          | Inbound                        | Outbound                      |
| ----------- | ------------------------------ | ----------------------------- |
| `sg-lambda` | (none)                         | All (will be tightened later) |
| `sg-aurora` | TCP 5432 from `sg-lambda` only | (none required)               |

### 6.4 Internet access for Lambda

Owner confirmed Lambdas do not need internet access in v1. So:

- **No NAT Gateway** (~$32/month saved per environment)
- **No internet routing** from private subnets
- Lambda can reach Aurora and nothing else

When S3 (photos) is added later:

- Add **S3 Gateway VPC Endpoint** — free, no ENI, just a route table entry

When Secrets Manager is adopted:

- Add **Secrets Manager Interface VPC Endpoint** (~$7/month per AZ)

### 6.5 Cold-start considerations

Lambda-in-VPC cold starts are no longer the legacy 10-second problem — AWS fixed this with Hyperplane ENI sharing in 2019. Current cold starts are roughly the same as non-VPC Lambda (200–800ms depending on runtime and package size). Acceptable for v1.

Mitigation if cold starts become an issue:

- Provisioned concurrency on the hot-path Lambdas (profile read, deck fetch)
- Smaller deployment packages (Lambda layers for heavy deps)
- ARM Graviton (`arm64`) for ~20% better price-performance

---

## 7. Security model

This section is the focus of the owner's MITM concern. Security is addressed at multiple layers.

### 7.1 Transport security (TLS)

All client-server traffic is HTTPS / WSS over TLS 1.2 minimum (TLS 1.3 preferred). This is non-negotiable and AWS-enforced for API Gateway, AppSync, Cognito, and S3.

### 7.2 Defenses against MITM specifically

A man-in-the-middle attack on a mobile app typically requires:

- A malicious WiFi network or compromised router, AND
- A rogue certificate trusted by the device (corporate MDM, jailbroken phone, user-installed CA)

Mitigations layered from outside to inside:

**(a) Certificate pinning (client-side)**

The mobile app pins the TLS certificate (or public key) of the API Gateway and AppSync endpoints. Even if a rogue CA is installed on the device, the app rejects any cert that doesn't match the pinned fingerprint.

Implementation: use `react-native-ssl-pinning` or AWS Amplify's HTTP client configuration. Pin the **public key SHA-256** of the AWS-managed certificate's intermediate CA (not the leaf cert, which rotates).

> **Trade-off**: pinning requires app updates when AWS rotates the intermediate CA. Pin two intermediate CAs (current + backup) to avoid emergency app releases.

**(b) Custom domain with managed certificate**

API Gateway and AppSync use custom domains (`api.knotify.app`, `chat.knotify.app`) backed by ACM certificates. This:

- Enables predictable cert rotation
- Makes pinning manageable
- Removes the AWS-generated execute-api hostname from the attack surface

**(c) Request signing (optional, strong)**

For maximum MITM resistance, add **HMAC-signed request bodies** in addition to JWT auth:

- Client computes `HMAC-SHA256(secret, body + timestamp + nonce)` and sends as `X-Signature` header
- Lambda authorizer rejects requests with stale timestamps (>5 min) or invalid signatures
- The shared secret is derived per-session from Cognito tokens

This is overkill for most apps and adds complexity. **Flagged for brainstorm — likely not needed in v1.**

**(d) Anti-replay**

Include `nonce` and `timestamp` in every request, rejected if seen before (Lambda authorizer maintains a short LRU cache in memory or in DynamoDB).

**(e) JWT validation hygiene**

The Lambda authorizer:

- Validates signature against Cognito JWKS (not just decoding the token)
- Checks `iss`, `aud`, `token_use = access`, `exp`, `nbf`
- Rejects tokens older than 1 hour
- Does NOT trust any claim outside the verified JWT (no header-based user_id)

**(f) WAF rules**

AWS WAF in front of API Gateway with:

- AWS Managed Rules: Core Rule Set, Known Bad Inputs, SQL Injection
- Rate-based rule: 100 requests per 5 min per IP for unauthenticated paths
- Geo-blocking if regional constraints exist
- Bot Control (paid, optional)

**(g) Mobile app hardening (out of backend scope but mentioned)**

- Disable root/jailbreak: use `jail-monkey` or similar to detect and refuse to run
- Code obfuscation (ProGuard for Android, Hermes bytecode for both)
- No secrets in app bundle
- Detect debugger attachment in production builds

### 7.3 AppSync / chat security (§8 expands)

See §8 — chat security is its own subsystem.

### 7.4 Aurora access security

- No public endpoint
- No internet routing from DB subnets
- IAM-authenticated connection optional (flagged); password auth via env vars in v1, Secrets Manager later
- SSL required on the connection (`sslmode=require` in psycopg2)
- All connections originate from Lambda security group only

### 7.5 Cognito hardening

- Advanced Security Features in prod (adaptive auth, compromised credential check)
- Account recovery via email only (not SMS — SIM-swap attack vector)
- Lockout after 5 failed login attempts
- Refresh token rotation enabled

### 7.6 Secret management

| Phase      | Approach                                                               |
| ---------- | ---------------------------------------------------------------------- |
| Solo dev   | DB password in Lambda env var (encrypted at rest with default KMS key) |
| Pre-launch | Migrate to Secrets Manager + VPC endpoint                              |
| Production | Rotate via Secrets Manager rotation Lambda; consider IAM DB auth       |

---

## 8. Chat security model (AppSync)

Chat is the most security-sensitive subsystem because:

- Real-time, hard to retroactively audit
- Messages are intended for specific recipients only
- A misconfigured subscription filter could leak conversations to wrong users

### 8.1 Authentication

- AppSync API auth mode: `AMAZON_COGNITO_USER_POOLS`
- Every connection (HTTPS or WSS) carries the Cognito JWT
- AppSync validates the token automatically against the user pool
- Identity is available in resolvers as `$ctx.identity.sub`

### 8.2 Authorization — per-mutation and per-subscription

**Critical lesson from the old design:** the old Amplify-generated schema auto-created subscriptions like `onCreateMessage`, `onUpdateMessage`, `onDeleteMessage` for every type. Without strict `@auth` directives (which were not visible in the old schema export), **any authenticated user could subscribe to every message in the system**. This is the single biggest security risk in Amplify auto-generation and must be explicitly mitigated in the rebuild.

**Mitigation in v2:** the GraphQL schema is written by hand (Terraform-managed `schema.graphql`), not auto-generated. It exposes **only the scoped subscriptions needed**:

```graphql
type Subscription {
  # Scoped: requires roomId, authorized against ChatRoomMembership
  onMessageInRoom(roomId: ID!): Message
    @aws_subscribe(mutations: ["sendMessage"])

  # Scoped: requires userId == identity.sub (cannot subscribe to others)
  onNotificationForMe: Notification
    @aws_subscribe(mutations: ["createNotification"])

  # Scoped to recipient only
  onTypingInRoom(roomId: ID!): TypingEvent

  # Scoped to participants of the deactivated room
  onRoomDeactivated(roomId: ID!): ChatRoom
}
```

No `onCreateUser`, no `onCreateMessage` (unscoped), no broad CRUD subscriptions. Every subscription has either an enforced `roomId`/`userId` argument or implicit identity scoping.

**Two threats to defend against:**

**Threat 1: User A sends a message claiming to be user B**

Defense: resolvers never accept a `sender_id` argument from the client. They always derive it from `$ctx.identity.sub`. The GraphQL schema reflects this:

```graphql
type Mutation {
  sendMessage(roomId: ID!, content: String!, contentType: ContentType!): Message
  # NOTE: no senderId argument. Server derives it from identity.
}
```

**Threat 2: User C subscribes to room R that they're not a participant in**

Defense: subscriptions use a **pipeline resolver** that, before establishing the subscription, verifies the requesting user is in `ChatRoomMembership` for the requested `roomId`.

Pipeline:

1. Client sends `subscription onMessageInRoom(roomId: $rid)`
2. AppSync runs the subscription's pipeline resolver
3. Resolver queries `ChatRoomMembership` (DynamoDB) with PK=`$ctx.identity.sub`, SK=`$rid`
4. If no membership row → reject subscription with Unauthorized
5. If row exists → establish, apply server-side filter so only messages where `room_id = $rid` are pushed
6. Additionally, the filter excludes messages from blocked users (denormalized blocks list cached per membership)

```graphql
type Subscription {
  onMessageInRoom(roomId: ID!): Message
    @aws_subscribe(mutations: ["sendMessage"])
}
```

### 8.3 Message integrity

- Every message has `sender_id` set server-side, never trusted from client
- `room_id` validated server-side against participant list before insert
- Optional: messages include an HMAC computed by the server, verified by the receiving client (defense against AppSync internal misrouting — but this is essentially trusting AWS, which is the security model anyway)

### 8.4 Content restrictions

- Text content: arbitrary UTF-8 (full multi-language, emoji support)
- GIFs: only URLs from a whitelisted provider (e.g., Giphy/Tenor — fetched server-side or with a `https://media.giphy.com/...` regex)
- No file attachments, no images, no videos, no documents
- Enforce at AppSync resolver: reject mutations with content not matching the allowed schema

### 8.5 Typing and read indicators

- `typing` mutation: ephemeral, no storage; pushes to subscribers via `onTyping` subscription
- `read_at` field on messages: updated via `markAsRead(roomId, lastMessageId)` mutation; pushes via `onReadReceipt` subscription

### 8.6 Room deactivation and reactivation flow

See §5.4.1 for the full lifecycle. Security-relevant points:

- **Deactivation triggers**: account deletion (irreversible, see §11) or block (reversible).
- **While deactivated**: `sendMessage` resolver rejects, but `onMessageInRoom` subscription remains open so the remaining party can see history.
- **Reactivation** only occurs on unblock by the same user who originally blocked, enforced by DynamoDB conditional `UpdateItem` checking `deactivated_reason = 'blocked'` AND `deactivated_by = current_user`.
- **No reactivation path for account-deletion deactivation** — the deleted user no longer exists, so there is no actor capable of re-enabling the room.

### 8.7 Block enforcement

Multi-layer:

1. **At room creation**: `createOrGetRoom(otherUserId)` resolver queries Aurora `blocks` table; if A→B or B→A exists, rejects with `BLOCKED` error before computing the room ID.
2. **At message send**: `sendMessage` resolver checks room status; if deactivated due to block, rejects.
3. **At room reactivation**: only the original blocker can reactivate (via unblock). If A blocked B, B cannot unblock themselves to "undo" the block.
4. **At feed/match level**: Aurora match queries always join against `blocks` (both directions) and exclude any user with a block relationship to the requester.

### 8.8 Defense against the deterministic room ID being "predictable"

A potential concern: since `room_id = sha256(canonical_pair(userA, userB))`, anyone who knows two user UUIDs could compute the room ID between them. **This is not a security issue** because:

- Knowing the room ID doesn't grant access. Membership is enforced by `ChatRoomMembership` lookups in every resolver.
- User UUIDs (Cognito `sub`) are not public; they're not exposed in API responses to non-friends.
- Even if someone obtains two UUIDs and computes a room ID, they cannot subscribe to that room (subscription resolver checks membership) nor send messages to it (mutation resolver checks membership).

Predictable IDs are only a problem when access control depends on ID secrecy. Knotify's access control depends on JWT identity and membership table lookups, not on ID secrecy.

---

## 9. Notifications

The full notification architecture — both in-app real-time and OS-level push — is documented in §5.4.2 as part of the chat data model section, since the two are tightly intertwined. This section covers only the push-provider choice.

### 9.1 In-app notifications inbox

See §5.4 (Notifications table schema) and §5.4.2 (end-to-end flow). DynamoDB table partitioned by `user_id`, time-sorted, 90-day TTL, written server-only by domain Lambdas in response to authenticated actions.

### 9.2 Push notification flow

See §5.4.2 Channel B. Triggered via DynamoDB Streams on both `ChatMessages` and `Notifications`; processed by a single `PushFanout` Lambda that handles both.

### 9.3 Push provider choice — Expo vs. SNS vs. FCM

| Option                                | Pros                                                          | Cons                                                                         |
| ------------------------------------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **Expo Push Notifications**           | Free, simple, works with Expo SDK, abstracts APNs/FCM         | Vendor lock-in to Expo; per-message rate limits; less control                |
| **AWS SNS Mobile Push**               | Native AWS, scales with the rest of the stack, no third party | More setup (APNs cert, FCM key); IAM permissions to manage; per-message cost |
| **Firebase Cloud Messaging directly** | Industry standard, free for unlimited use                     | Adds Google as another vendor; need to bridge into AWS for stats             |
| **OneSignal / Pusher / similar**      | Rich analytics, A/B testing                                   | Third-party; cost; another vendor                                            |

**Recommendation: Expo Push for v1** if the app is built with Expo (typical for modern React Native). Trivial integration via `expo-notifications` library + Expo's HTTP API. Can swap to SNS later by changing the `PushFanout` Lambda's outbound call.

If the app is bare React Native (not Expo), use **AWS SNS Mobile Push**.

**RESOLVED: Expo Push** (per owner decision). The mobile app is Expo-managed, so `expo-notifications` on the client and Expo's HTTP push API on the backend is the natural fit. The `PushFanout` Lambda's outbound call targets Expo. SNS Mobile Push remains a viable swap if Expo limits become an issue — only the outbound call inside the Lambda changes.

### 9.4 Push token storage

See §5.4 `PushNotificationTokens` table schema.

Token lifecycle:

- Mobile app registers token on startup (after user grants notification permission)
- App calls a small REST endpoint `POST /v1/push-tokens` with `{platform, push_token, device_id, app_version}` to upsert
- Tokens stale for >60 days are cleaned by scheduled Lambda
- Tokens that Expo/SNS report as "DeviceNotRegistered" are deleted immediately by the `PushFanout` Lambda

---

## 10. Deployment — Terraform + GitHub Actions

### 10.1 Terraform layout

```
infrastructure/
├── modules/
│   ├── networking/         # VPC, subnets, security groups
│   ├── cognito/            # User pool, app clients
│   ├── api_gateway/        # REST API, stages, authorizers
│   ├── lambda/             # Functions, roles, log groups
│   ├── aurora/             # Cluster, parameter group, secrets
│   ├── dynamodb/           # All tables
│   ├── appsync/            # GraphQL API, schema, resolvers
│   ├── s3/                 # Buckets (when added)
│   └── waf/                # WAF rules + association
├── environments/
│   ├── dev/
│   │   ├── main.tf
│   │   ├── terraform.tfvars
│   │   └── backend.tf      # S3 backend in dev account
│   └── prod/
│       ├── main.tf
│       ├── terraform.tfvars
│       └── backend.tf      # S3 backend in prod account
└── tests/                  # Terraform native test files (.tftest.hcl)
```

### 10.2 GitHub Actions pipeline

**Routing rule, stated plainly:**

| Trigger                                                 | Target environment | Notes                                               |
| ------------------------------------------------------- | ------------------ | --------------------------------------------------- |
| Push to `development` branch                            | dev AWS account    | Auto-deploys after tests pass                       |
| Push to `main` branch                                   | prod AWS account   | Auto-deploys after **manual approval** in GitHub UI |
| Pull request opened/updated against `development` or `main` | Neither            | Runs validate + plan + test only; never applies     |
| Any other branch (`feat/*`, etc.)                       | Neither            | No CI fires unless a PR is opened                   |

**Full workflow file** (`.github/workflows/deploy.yml`):

```yaml
name: Deploy
on:
  push:
    branches: [main, development]
  pull_request:
    branches: [main, development]

permissions:
  contents: read
  id-token: write # for OIDC if adopted; harmless otherwise

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: 1.7.5
      - run: terraform fmt -check -recursive infrastructure/
      - run: |
          for env in dev prod; do
            terraform -chdir=infrastructure/environments/$env init -backend=false
            terraform -chdir=infrastructure/environments/$env validate
          done
      - uses: terraform-linters/setup-tflint@v4
      - run: tflint --recursive
      - uses: aquasecurity/tfsec-action@v1.0.3

  plan:
    needs: validate
    runs-on: ubuntu-latest
    strategy:
      matrix:
        # Plan for both environments on every PR/push so reviewers see both diffs
        environment: [dev, prod]
    environment: ${{ matrix.environment }} # picks env-scoped secrets
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: 1.7.5
      - env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        run: |
          terraform -chdir=infrastructure/environments/${{ matrix.environment }} init
          terraform -chdir=infrastructure/environments/${{ matrix.environment }} plan -out=tfplan
      - uses: actions/upload-artifact@v4
        with:
          name: tfplan-${{ matrix.environment }}
          path: infrastructure/environments/${{ matrix.environment }}/tfplan

  test:
    needs: plan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform -chdir=infrastructure/ test

  apply-dev:
    needs: test
    if: github.event_name == 'push' && github.ref == 'refs/heads/development'
    runs-on: ubuntu-latest
    environment: dev
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - uses: actions/download-artifact@v4
        with:
          name: tfplan-dev
          path: infrastructure/environments/dev/
      - env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        run: |
          terraform -chdir=infrastructure/environments/dev init
          terraform -chdir=infrastructure/environments/dev apply tfplan

  apply-prod:
    needs: test
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: prod # has required-reviewers protection → manual approval
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - uses: actions/download-artifact@v4
        with:
          name: tfplan-prod
          path: infrastructure/environments/prod/
      - env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        run: |
          terraform -chdir=infrastructure/environments/prod init
          terraform -chdir=infrastructure/environments/prod apply tfplan
```

**Key mechanics:**

- The `if:` conditions on `apply-dev` and `apply-prod` are how the branch decides the environment — `development` only triggers `apply-dev`, `main` only triggers `apply-prod`. A push to any other branch triggers nothing past validation.
- `environment: dev` / `environment: prod` activates the corresponding GitHub Environment, which is what supplies the right secrets and enforces the manual approval gate (only configured on `prod`).
- The plan job runs for **both** environments on every push/PR — this lets you see "what would change in dev _and_ what would change in prod" before merging. Important: even when changing dev-only Terraform, the prod plan should show "no changes."

### 10.3 Environment routing — how you make the deploy choice day-to-day

You don't pick the environment via a dropdown or input. **The branch you push to is the choice.**

A typical iteration looks like:

```
1. Create feature branch
   git checkout -b feat/add-blocks-endpoint
   ... make changes ...
   git push -u origin feat/add-blocks-endpoint
   → CI runs: validate + plan (both envs) + test. No deploy.

2. Open PR to development
   gh pr create --base development
   → Same checks re-run. Reviewer (you) sees plan output in PR comments.
   → Merge when satisfied.

3. Merge triggers dev deploy
   → Push to development fires the full pipeline.
   → apply-dev job runs automatically. Dev AWS account updated.
   → No approval needed for dev.

4. Test in the dev environment.
   → Real React Native app pointed at dev API. Manually verify.

5. Promote to prod
   gh pr create --base main --head development --title "Release v1.2.0"
   → CI runs again on the promotion PR. Plan shows what will change in prod.
   → Merge.

6. Push to main triggers prod deploy
   → apply-prod job is queued.
   → GitHub UI shows "Waiting for review" — you must click "Review deployments"
     and approve before it runs.
   → After approval, terraform apply runs against prod account.
```

**The choice points where you have direct control:**

- Which branch you merge into (`development` vs. `main`) — picks the target
- Whether to approve the prod gate when it appears — final go/no-go

**Branch protection rules** (configured manually in GitHub repo settings):

- `main` branch: require PR, require status checks (validate, plan, test) to pass, no direct pushes
- `development` branch: same, but slightly looser if you want (e.g., allow your own direct pushes for fast iteration)

### 10.4 Secrets in GitHub Actions

**Use GitHub Environment secrets, not repository secrets.** This is the mechanism that lets the _same secret name_ resolve to _different values_ depending on which environment the job runs in.

Setup (in repo settings → Environments):

| GitHub Environment | Secret name             | Value                     |
| ------------------ | ----------------------- | ------------------------- |
| `dev`              | `AWS_ACCESS_KEY_ID`     | (dev account access key)  |
| `dev`              | `AWS_SECRET_ACCESS_KEY` | (dev account secret)      |
| `prod`             | `AWS_ACCESS_KEY_ID`     | (prod account access key) |
| `prod`             | `AWS_SECRET_ACCESS_KEY` | (prod account secret)     |

In the workflow, `secrets.AWS_ACCESS_KEY_ID` resolves to **the dev value when `environment: dev` is active**, and the prod value when `environment: prod` is active. The same workflow code targets different accounts without `if`-conditioned secret picking.

This also means prod credentials are inaccessible to jobs that don't declare `environment: prod` — a job running on a PR cannot reach prod credentials even if compromised, because it never enters the prod environment context.

**Recommended upgrade (flagged §13 #1):** replace static access keys with GitHub OIDC federation. Same routing structure, but `secrets.AWS_ACCESS_KEY_ID` becomes a role ARN that GitHub assumes via short-lived credentials. No rotation needed, no static secrets to leak.

### 10.5 Terraform test coverage (v1)

Native Terraform tests cover:

- Module input validation (does the VPC module reject bad CIDRs?)
- Resource shape (does the Aurora cluster have the right engine version?)
- Plan-time assertions (no public-accessible RDS, all S3 buckets block public access)

No post-deployment smoke tests in v1 per owner preference.

### 10.6 Pre-implementation smoke test (required before phase 1 begins)

**Purpose:** before any real infrastructure is written, prove end-to-end that the deployment pipeline works. Do not begin §11+ implementation until this is green in both environments.

**Why this matters:** the pipeline has many integration points where each one can fail silently — AWS Organization setup, member account IAM, GitHub Actions secrets, Terraform backend in S3 + DynamoDB, OIDC trust (if used) or static-key auth, region selection, provider versions. A real implementation that hits one of these in the middle of building Aurora is hard to debug. Validating with a trivial deploy first isolates the pipeline from the application logic.

**Scope of the smoke test — deliberately minimal:**

- One Terraform module that creates **one S3 bucket** with `block_public_access = true`, server-side encryption, a unique name including environment (e.g., `knotify-smoke-dev-<random_suffix>`, `knotify-smoke-prod-<random_suffix>`).
- That's it. No VPC, no Lambda, no Aurora, no Cognito, no IAM beyond what's needed for the bucket itself.

**What the smoke test verifies:**

| Check                                                 | How                                                                                              |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| AWS Organization is set up with dev + prod accounts   | Manual sanity check in console                                                                   |
| Programmatic credentials (or OIDC role) work          | GitHub Actions can successfully call AWS APIs                                                    |
| GitHub Actions secrets are configured per environment | Job picks the right credentials based on branch/environment                                      |
| Terraform backend (S3 + DynamoDB lock) works          | `terraform init` succeeds; state is written to S3; lock is acquired and released                 |
| Region is configured correctly                        | Bucket created in expected region (recommend `eu-central-1` for owner's location)                |
| Provider versions resolve                             | `terraform init` downloads providers without conflicts                                           |
| Plan → Apply flow works end-to-end                    | `terraform apply` completes without manual intervention                                          |
| Environment separation works                          | Pushing to `development` deploys to dev account only; pushing to `main` deploys to prod account only |
| Manual approval gate on prod works                    | The prod job blocks until approved in GitHub Environments UI                                     |
| Destroy works (cleanup)                               | A `terraform destroy` job can run on demand to remove the smoke-test bucket                      |

**Workflow definition (illustrative):**

```yaml
# .github/workflows/smoke-test.yml
name: Smoke Test
on:
  workflow_dispatch:
  push:
    branches: [smoke-test/*]
    paths: ["infrastructure/smoke/**"]

jobs:
  smoke-dev:
    if: github.ref == 'refs/heads/smoke-test/dev'
    runs-on: ubuntu-latest
    environment: dev # picks up dev environment secrets
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform -chdir=infrastructure/smoke init
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
      - run: terraform -chdir=infrastructure/smoke plan
      - run: terraform -chdir=infrastructure/smoke apply -auto-approve

  smoke-prod:
    if: github.ref == 'refs/heads/smoke-test/prod'
    runs-on: ubuntu-latest
    environment: prod # gates on GitHub Environments manual approval; picks up prod secrets
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - run: terraform -chdir=infrastructure/smoke init
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
      - run: terraform -chdir=infrastructure/smoke plan
      - run: terraform -chdir=infrastructure/smoke apply -auto-approve
```

**Terraform (the actual smoke module — `infrastructure/smoke/main.tf`):**

```hcl
terraform {
  required_version = ">= 1.7"
  required_providers {
    aws    = { source = "hashicorp/aws",    version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {
    # backend.tf per environment fills in bucket, key, region, dynamodb_table
  }
}

provider "aws" {
  region = var.region
}

variable "environment" { type = string }
variable "region"      { type = string  default = "eu-central-1" }

resource "random_id" "suffix" { byte_length = 4 }

resource "aws_s3_bucket" "smoke" {
  bucket = "knotify-smoke-${var.environment}-${random_id.suffix.hex}"
  tags = {
    Project     = "knotify"
    Environment = var.environment
    Purpose     = "pipeline-smoke-test"
    ManagedBy   = "terraform"
  }
}

resource "aws_s3_bucket_public_access_block" "smoke" {
  bucket                  = aws_s3_bucket.smoke.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "smoke" {
  bucket = aws_s3_bucket.smoke.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

output "bucket_name" { value = aws_s3_bucket.smoke.id }
```

**Procedure (the order the owner should run these in):**

1. **Set up AWS Organization** with two member accounts (dev, prod) manually in the console
2. **In each member account, create an IAM user** with programmatic access keys (or, recommended, set up OIDC trust to GitHub — §3.2)
3. **Manually create the Terraform backend** in each account:
   - One S3 bucket for state (`knotify-tfstate-dev`, `knotify-tfstate-prod`) with versioning enabled
   - One DynamoDB table for state locking (`knotify-tfstate-lock`)
   - These are bootstrap resources and are NOT managed by Terraform (chicken-and-egg)
4. **Configure GitHub repository** with:
   - Two **environments**: `dev` and `prod`, with prod requiring manual approval
   - Per-environment **secrets**: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (or OIDC role ARN)
5. **Create the smoke test branches**:
   - Push the smoke module to `smoke-test/dev` → triggers dev deploy → expect success → verify bucket exists in dev account
   - Push the smoke module to `smoke-test/prod` → triggers prod deploy → manually approve in GitHub UI → verify bucket exists in prod account
6. **Run destroy** to clean up the smoke buckets in both accounts
7. **Commit a "smoke test passed" note** somewhere visible (e.g., a `PIPELINE_VALIDATED.md` checked in to the repo root with the date and outputs); only after this proceed to phase 1

**Estimated time:** 2–4 hours for someone new to this stack, mostly spent on the manual AWS setup steps. The Terraform module itself takes 30 minutes.

**Failure modes this catches:**

- "GitHub Actions cannot assume role" — usually a typo in the OIDC trust policy or wrong account ID
- "Backend init fails: NoSuchBucket" — backend bucket was created in the wrong region or hasn't been created
- "DynamoDB lock conflict" — lock table doesn't exist or wrong name
- "Prod credentials used in dev" — environment secrets not scoped correctly
- "Apply fails: account is not part of organization" — organization linkage misconfigured
- "Terraform provider version drift" — `required_providers` not pinned, weird behavior on subsequent runs

**Once the smoke test passes** in both environments, the pipeline is proven and real implementation can begin. The smoke module can be deleted from the repo (or kept under `infrastructure/smoke/` as a future regression test if AWS credentials ever change).

---

## 11. Account deletion flow

Owner-specified requirement: when a user deletes their account, all their data is removed and chat rooms they were part of are deactivated.

### 11.1 Deletion procedure (transactional where possible, choreographed otherwise)

Triggered by `DELETE /v1/profile/me`:

1. **Cognito**: delete the user from the user pool (or disable + schedule deletion)
2. **Aurora** — **soft delete chosen** (per owner decision, §13 #8):
   - `UPDATE users SET deleted_at = NOW(), email = NULL, phone_number = NULL, photo_url = NULL, chosen_profile_avatar = NULL, preferences = '{}'::jsonb, preference_vector = NULL WHERE user_id = :id` — strips PII immediately while preserving the row for referential integrity (friendships, bookmarks, blocks, friend_requests remain intact for the surviving party's view; the soft-deleted user is filtered from every read by the existing `WHERE deleted_at IS NULL` predicate and the `deck_view` definition)
   - The `users` row also receives a sentinel for display: `username = '[deleted-user]'`, `first_name = 'Deleted'`, `last_name = 'User'` — so any surviving denormalized references render gracefully
   - Soft-deleted rows are purged by a **scheduled Lambda** after a fixed retention window of **30 days** — long enough to recover from accidental deletion, short enough to satisfy GDPR "reasonable timeframe"
   - On final purge: `DELETE FROM users WHERE deleted_at < NOW() - INTERVAL '30 days'` — `ON DELETE CASCADE` then removes siblings, friendships, friend_requests, bookmarks, blocks
   - Hard delete remains the fallback for explicit GDPR "right to be forgotten" requests requiring immediate purge — the same Step Functions workflow accepts a `purge_immediately: true` flag that skips the 30-day wait
3. **DynamoDB**:
   - For each room in `ChatRooms` where user is participant:
     - Set `status = 'deactivated'`, `deactivated_reason = 'user_deleted_account'`
     - Publish AppSync `roomDeactivated` event to remaining participant
   - Delete entries from `Notifications`, `PushTokens` for the user
4. **S3** (when implemented): delete all photos under `users/{user_id}/`
5. **Audit log**: write deletion event to immutable audit table (DynamoDB, with TTL >7 years if compliance requires)

### 11.2 Step Functions orchestration

The deletion is implemented as an **AWS Step Functions Standard workflow**. A REST endpoint (`DELETE /v1/profile/me`) triggers the workflow asynchronously, returning `202 Accepted` immediately with an execution ARN the client can poll if desired. The actual deletion runs in the background, durably, with automatic retries and a full audit trail.

**Why Step Functions instead of a single Lambda?**

- **Durability**: Step Functions Standard workflows can run for up to a year. A single Lambda is capped at 15 minutes and dies if anything goes wrong mid-flight.
- **Retries**: each step has independent retry config with exponential backoff. A flaky DynamoDB call doesn't restart the whole flow.
- **Visibility**: the AWS console shows each execution as a visual graph with the status of every step — invaluable when debugging a partial deletion.
- **Idempotency**: each Lambda step is written to be safe to re-run. Step Functions handles "did this step succeed?" tracking automatically.
- **Compensation**: if a late step fails permanently, the workflow can route to a `Catch` branch that alerts ops without leaving the user in a half-deleted state.
- **Cost**: at this volume (deletion is rare), Step Functions costs are negligible — about $0.025 per 1000 state transitions.

**Workflow definition (state machine):**

```
┌──────────────────────────────────────────────────────────────────────┐
│  StartAt: ValidateDeletionRequest                                    │
│                                                                      │
│  ValidateDeletionRequest (Lambda)                                    │
│    - Confirm user_id from Cognito JWT matches request                │
│    - Check no active deletion in progress (idempotency)              │
│    - Write "deletion_initiated" to audit log                         │
│    → next: DisableCognitoUser                                        │
│                                                                      │
│  DisableCognitoUser (Lambda, retries: 3, backoff: 2s)                │
│    - AdminDisableUser in Cognito (prevents new logins)               │
│    - Do NOT delete from Cognito yet (need user_id for cascades)      │
│    → next: DeactivateChatRooms                                       │
│                                                                      │
│  DeactivateChatRooms (Lambda, retries: 5, backoff: 2s)               │
│    - Query DynamoDB for all rooms where user is participant          │
│    - For each room: set status='deactivated',                        │
│      deactivated_reason='user_deleted_account'                       │
│    - Publish AppSync `roomDeactivated` event to remaining participant│
│    - Idempotent: re-runs skip rooms already deactivated              │
│    → next: ParallelCleanup                                           │
│                                                                      │
│  ParallelCleanup (Parallel state — branches run concurrently)        │
│    ├── Branch A: DeleteFromAurora (Lambda)                           │
│    │     - BEGIN TRANSACTION                                         │
│    │     - DELETE FROM users WHERE user_id = :id                     │
│    │       (cascades: siblings, friendships, friend_requests,        │
│    │        bookmarks, blocks)                                       │
│    │     - COMMIT                                                    │
│    │                                                                 │
│    ├── Branch B: DeleteDynamoDBData (Lambda)                         │
│    │     - Delete Notifications for user_id                          │
│    │     - Delete PushTokens for user_id                             │
│    │     - Delete ChatMessages where sender_id = user_id (optional)  │
│    │       (or anonymize: set sender_id='[deleted-user]')            │
│    │                                                                 │
│    └── Branch C: DeleteS3Photos (Lambda, when S3 is added)           │
│          - List objects under users/{user_id}/                       │
│          - Batch delete (up to 1000 at a time)                       │
│                                                                      │
│  → next: DeleteCognitoUser                                           │
│                                                                      │
│  DeleteCognitoUser (Lambda, retries: 3)                              │
│    - AdminDeleteUser in Cognito (now safe, all cascades done)        │
│    → next: WriteAuditLog                                             │
│                                                                      │
│  WriteAuditLog (Lambda)                                              │
│    - Write "deletion_completed" record to immutable audit table      │
│      (DynamoDB, retention 7 years for GDPR proof)                    │
│    - Record: user_id, completed_at, branches_succeeded               │
│    → next: NotifySuccess                                             │
│                                                                      │
│  NotifySuccess (SNS publish)                                         │
│    - Optional: email confirmation to deleted user's old email        │
│    → END (success)                                                   │
│                                                                      │
│  Catch (global): DeletionFailed                                      │
│    - Triggered if any step exhausts retries                          │
│    - Write "deletion_failed" + step_name + error to audit log        │
│    - SNS alert to ops email                                          │
│    - Do NOT mark user as deleted; leave for manual investigation     │
│    → END (failure, manual intervention required)                     │
└──────────────────────────────────────────────────────────────────────┘
```

**Key design decisions in the workflow:**

1. **Disable before delete in Cognito** — this prevents the user from logging in mid-deletion and seeing a broken state, but keeps the user_id available so that downstream Lambdas can reference it for cascade deletes. Cognito deletion is the last step.

2. **Aurora deletes use ON DELETE CASCADE** — the schema in §5.1 defines foreign keys with cascade, so a single `DELETE FROM users` removes siblings, friendships, friend_requests, bookmarks, and blocks atomically.

3. **Chat rooms are deactivated, not deleted** — preserves message history for the remaining participant. The remaining user sees a "User has deleted their account" message in the room. Messages remain readable; new messages are rejected at the AppSync resolver level (room status check).

4. **ChatMessages sender_id handling** — **RESOLVED: anonymize** (per owner decision, §13 #21). The `DeleteDynamoDBData` step rewrites each of the deleted user's `ChatMessages` rows: `sender_id` → sentinel `'[deleted-user]'`, leaving `content`, `delivered_at`, `room_id`, and the sort key intact. The surviving participant continues to read the room normally; the mobile UI substitutes "Deleted User" wherever `sender_id == '[deleted-user]'`. This preserves conversation continuity for the surviving party while satisfying PII removal (the original `sender_id` UUID is no longer reachable from the message row).

5. **Parallel cleanup** — Aurora, DynamoDB, and S3 deletes are independent and run concurrently to minimize total deletion time. If one fails, Step Functions routes to the global `Catch` without rolling back the others — partial cleanup is preferable to no cleanup, and the workflow can be re-run idempotently.

6. **Idempotency at every step** — re-running a partially completed workflow is safe. Each Lambda checks "is this already done?" before acting. This means the same workflow can be retried manually after fixing a transient issue.

7. **Audit log is append-only** — even after the user is deleted, the audit record `(user_id, deleted_at, completed)` persists for legal/compliance purposes. This is allowed under GDPR's "legitimate interest" exception.

**Express vs. Standard workflow:**

- Use **Standard** workflows here, not Express. Standard is for long-running, durable, audited workflows (up to 1 year, full execution history retained). Express is for high-volume, short-duration (≤5 min), best-effort workflows. Account deletion is rare, needs durability and audit trail — Standard is the right choice.

**Cost estimate:**

At 100 account deletions/month (generous), each workflow has ~8 state transitions:

- 800 transitions/month × $0.025/1000 = **$0.02/month**

Effectively free.

**Triggering from the API:**

```
DELETE /v1/profile/me
  ↓
Lambda (DeletionInitiator):
  - Validates JWT
  - Calls StartExecution on the Step Functions state machine
  - Returns 202 Accepted with executionArn
```

The mobile client can:

- Trust the 202 and log the user out immediately (typical UX)
- Optionally poll a `GET /v1/profile/me/deletion-status` endpoint to confirm completion
- Receive a confirmation push notification when deletion completes (if push token still valid)

### 11.3 GDPR / legal considerations

- Right to be forgotten requires actual deletion (not just soft-delete) within a reasonable timeframe
- Backups: Aurora automated backups retain deleted data for the backup retention window (7 days dev, 30 days prod). Document this in the privacy policy.
- Audit log of "user X deleted at time T" is allowed under legitimate interest

---

## 12. Cost model

Reference costs (us-east-1, May 2026).

### 12.1 Per environment, monthly

| Component                                     | Solo dev (auto-pause) | Friends beta (0.5 ACU warm) | 10K-user launch |
| --------------------------------------------- | --------------------- | --------------------------- | --------------- |
| Aurora Serverless compute                     | $18                   | $45                         | $105            |
| Aurora storage (5–50 GB)                      | $0.50                 | $1                          | $5              |
| Aurora I/O                                    | $0                    | $2                          | $15             |
| DynamoDB (chat + notifications)               | $1                    | $5                          | $30–80          |
| HTTP API Gateway (req-based)                  | $0                    | $0–2                        | $5–30           |
| CloudFront (req + data transfer)              | $0–1                  | $1–5                        | $5–15           |
| Lambda (invocations + GB-sec)                 | $0–5                  | $5–15                       | $30–80          |
| AppSync (queries + subs + connection minutes) | $0–2                  | $5–20                       | $30–100         |
| Step Functions (account deletion)             | $0                    | $0                          | <$1             |
| Cognito (MAU)                                 | Free tier (50k MAU)   | Free                        | Free            |
| WAF (attached to CloudFront)                  | $5 base + $1/rule     | $7                          | $10             |
| CloudWatch logs + metrics                     | $1–5                  | $5–10                       | $15–30          |
| Data transfer                                 | Negligible            | $1–5                        | $10–30          |
| **Total per environment**                     | **~$25–30**           | **~$75**                    | **~$200–380**   |

Two environments = roughly **2× the dev cost** during development, since prod is mostly idle until launch.

### 12.2 One-time / occasional

- Route 53 hosted zone: $0.50/month + queries
- ACM certificates: free
- Domain registration: ~$12/year

---

## 13. Open questions for brainstorm agent

The following decisions are explicitly deferred. The brainstorm agent should probe them and recommend resolution before implementation begins.

1. ~~**GitHub OIDC vs. static keys** (§3.2).~~ **RESOLVED**: static keys for v1; OIDC migration deferred to the pre-launch hardening phase. See §3.2.
2. ~~Lambda runtime: Python 3.12 or Node.js 20?~~ **RESOLVED**: Python 3.14 on ARM64 (Graviton) for all Lambdas including AppSync resolvers, Step Functions tasks, and Cognito triggers. Owner confirmed.
3. **One Lambda per domain vs. per route**: trade off cold start surface vs. deployment granularity. Current proposal: per-domain (§4.2 migration map).
4. ~~**`deck_view`**: materialized view vs. regular view vs. denormalized table?~~ **RESOLVED**: materialized view. See §5.1.
5. ~~**Gender isolation**: RLS vs. physical table separation?~~ **RESOLVED**: PostgreSQL Row-Level Security on a single `users` table. See §5.2.
6. ~~Notifications inbox: DynamoDB vs. Aurora~~ **RESOLVED**: DynamoDB (confirmed by old schema review — §5.4, §5.6).
7. ~~**Push notifications**: Expo vs. SNS?~~ **RESOLVED**: Expo Push (Expo-managed mobile app). See §9.3.
8. ~~**Account deletion**: hard delete vs. soft delete?~~ **RESOLVED**: soft delete with 30-day retention, followed by hard purge via a scheduled Lambda. Immediate hard delete remains available via a `purge_immediately` flag for GDPR right-to-be-forgotten requests. See §11.1.
9. **HMAC request signing**: include in v1 or defer? Recommendation: defer; rely on TLS + JWT + pinning.
10. **Custom domain naming**: `api.knotify.app` / `chat.knotify.app` — confirm owner has domain control.
11. ~~API surface~~ **PARTIALLY RESOLVED**: derived from the old OpenAPI spec and adapted to HTTP API conventions (§4.2 migration map). Owner to confirm endpoint list; specifically the role of `GET /v1/feed` (old `/newsFeed`) is unclear and may be removable.
12. ~~REST API vs. HTTP API~~ **RESOLVED**: HTTP API + CloudFront + WAF chosen. ~70% cheaper, better edge protection, lower latency. See §4.2.
13. ~~ChatRoom membership index: 2 GSIs vs. separate membership table~~ **RESOLVED**: `ChatRoomMembership` join table (§5.4). Confirmed by old schema review — old design used the same pattern (`ChatRoomUser`).
14. **Match scoring weights**: cosine similarity over the 20-D vector is naive — should `religion`, `age proximity`, `education` carry weight? If yes, the vector should be wider and the encoder needs to assign weights.
15. **Profanity / content moderation for chat**: defer to v2 or include in v1?
16. **GIF provider**: Giphy, Tenor, or own curation? Affects URL whitelisting.
17. **Backup strategy for DynamoDB**: PITR + on-demand backups, and retention?
18. ~~**Observability stack**: CloudWatch only, or add Sentry / Datadog?~~ **RESOLVED**: CloudWatch only for v1. Implementation strategy: every Lambda ships from birth with the AWS Lambda Powertools structured-logging layer, correlation IDs, and metric emission. A dedicated late-stage observability phase consolidates CloudWatch alarms (Lambda errors > 1%, throttles, p99 duration anomalies, Aurora CPU/connections, DynamoDB throttles, API Gateway 5xx), SNS topic + email subscription for alarm routing, and one CloudWatch dashboard per environment. **Log retention: 7 days for all CloudWatch log groups in both dev and prod** (per owner cost-control directive — storage accumulation avoided). Sentry/Datadog deferred indefinitely.
19. **Rate limiting per user (not just per IP)**: implemented in Lambda authorizer using DynamoDB counters? (Requires moving from built-in JWT authorizer to Lambda authorizer for the affected routes.)
20. **Email/SMS for transactional notifications** (e.g., "your account was deleted"): SES setup needed?
21. ~~**ChatMessages on user deletion**: delete vs. anonymize?~~ **RESOLVED**: anonymize `sender_id` to the sentinel `'[deleted-user]'`; preserve `content`, `delivered_at`, and message ordering. UI substitutes "Deleted User" on display. See §11.2 #4.
22. **CloudFront origin secret rotation**: how often is the secret header between CloudFront and HTTP API rotated, and via what mechanism (manual, Secrets Manager + Lambda)?
23. **`/v1/feed` endpoint** (old `/newsFeed?feedtype=...`): purpose unclear from the old OpenAPI spec. Possibly an announcements/admin feed? May be removable from v2 scope. Owner to confirm.
24. **`/userverificationdocs` endpoint** (old): document upload — what kind of documents and verification? Deferred to phase 2 (S3 + photo uploads), but the verification flow itself is undefined. Manual admin review? Automated ML?
25. **Message edits**: were edits supported in the old app? Old schema had `updateMessage` and `deleteMessage` mutations (auto-generated). Decide whether v2 supports message editing/deletion by user, and how it interacts with read receipts.
26. **Cognito post-confirmation Lambda trigger**: confirm we use this pattern to pre-create Aurora `users` row at signup (avoids the "Cognito user without profile" half-state).
27. **`PreferredUsername` Cognito attribute**: the old Amplify config showed `PREFERRED_USERNAME` as a signup attribute alongside `EMAIL`. Is preferred_username going to be used in v2? Affects the `username` field handling in Aurora.
28. **Push debouncing**: should `PushFanout` Lambda debounce rapid-fire pushes (e.g., 50 messages from one sender in 30 seconds collapsed into one push)? See §5.4.2 "What if Bob spams 50 messages?". Recommendation: defer to v2; rely on Expo's device-side deduplication initially.

---

## 14. Glossary

- **ACU** — Aurora Capacity Unit; ~2 GB memory + corresponding CPU. Billing unit for Aurora Serverless.
- **AppSync** — AWS managed GraphQL service with real-time subscriptions.
- **Cognito sub** — Subject claim in Cognito JWT; canonical user identifier.
- **ENI** — Elastic Network Interface; how Lambda attaches to a VPC.
- **HNSW** — Hierarchical Navigable Small World; pgvector's preferred index for high-recall ANN search.
- **JWKS** — JSON Web Key Set; public keys used to verify JWT signatures.
- **MITM** — Man-in-the-middle attack.
- **OIDC** — OpenID Connect; federation protocol for short-lived credentials.
- **pgvector** — PostgreSQL extension for vector similarity search.
- **RLS** — Row-Level Security; PostgreSQL feature for per-row access policies.
- **TTL** — Time to live; DynamoDB attribute for automatic expiration.

---

_End of architecture document._
