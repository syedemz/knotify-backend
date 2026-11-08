project: knotify-backend
last_updated: 2026-11-08 # phase 14 (Supabase alternative to Aurora RDS + private VPC) added; PRD + detailed TDD plan in features/feature-1-*; see implementationplan/phase-14-supabase-alternative.md

phases:

- phase: 0
  title: Pipeline smoke test
  file: implementationplan/phase-0-smoke-test.md
  ready: true
  done: true

- phase: 1
  title: Networking foundations
  file: implementationplan/phase-1-networking.md
  ready: true
  done: true

- phase: 2
  title: Data layer (Aurora + DynamoDB)
  file: implementationplan/phase-2-data-layer.md
  ready: true
  done: true

- phase: 3
  title: Lambda foundations
  file: implementationplan/phase-3-lambda-foundations.md
  ready: true
  done: true

- phase: 4
  title: Cognito
  file: implementationplan/phase-4-cognito.md
  ready: true
  done: true

- phase: 5
  title: API edge (HTTP API + CloudFront + WAF)
  file: implementationplan/phase-5-api-edge.md
  ready: true
  done: true

- phase: 6
  title: Profile, friends, bookmarks, blocks domain Lambdas
  file: implementationplan/phase-6-domain-lambdas.md
  ready: true
  done: true

- phase: 7
  title: Match and deck
  file: implementationplan/phase-7-match.md
  ready: true
  done: true

- phase: 8
  title: Chat (AppSync + DynamoDB Streams + push fan-out)
  file: implementationplan/phase-8-chat.md
  ready: true
  done: true

- phase: 9
  title: Account deletion (Step Functions, soft delete)
  file: implementationplan/phase-9-account-deletion.md
  ready: true
  done: true

- phase: 10
  title: Observability consolidation
  file: implementationplan/phase-10-observability.md
  ready: false
  done: false

- phase: 11
  title: Pre-launch hardening
  file: implementationplan/phase-11-hardening.md
  ready: false
  done: false

- phase: 12
  title: S3 photos and uploads
  file: implementationplan/phase-12-s3-photos.md
  ready: false
  done: false

- phase: 14
  title: Supabase alternative to Aurora RDS and private VPC (dev cost reduction; prod-capable)
  file: implementationplan/phase-14-supabase-alternative.md
  ready: false
  done: false
  # Motivation: October 2026 AWS bill was $207, of which Aurora + VPC = ~$167 (81%).
  # Mutually-exclusive Terraform flag use_supabase. Dev defaults to true (Aurora torn down 2026-11-08);
  # prod pinned false (safety) but architecturally symmetric so a future prod cutover is a
  # one-tfvar flip + data-migration PRD.
  # Full PRD: features/feature-1-supabase-alternative.md
  # Detailed TDD plan: features/feature-1-supabase-alternative-plan.md
  # Note: phase 13 (premium subscriptions) PRD file exists at implementationplan/phase-13-premium-subscriptions.md
  # but is not currently indexed here — pre-existing inconsistency, not introduced by this phase.
