phase: 12
title: S3 photos and uploads
last_updated: 2026-05-21

context_summary: |
  Adds S3-backed profile photos and verification document upload — the capability architecture.md §4.8 originally labeled "phase 2" and deferred from MVP. One S3 bucket per environment, all public access blocked, server-side encryption, lifecycle rule deleting orphaned uploads after 24h, S3 Gateway VPC endpoint so Lambdas can reach the bucket without internet egress. The knotify-uploads Lambda returns pre-signed URLs; CloudFront serves reads through signed URLs. The knotify-profile PATCH handler from phase 6 is extended to accept photo_url updates after a successful upload. This phase has no dependents within the current plan; it is the final capability after hardening.

stories:
  - id: 12.1
    title: S3 bucket Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/s3/main.tf creates aws_s3_bucket "knotify-uploads-${var.environment}-${random_id.suffix.hex}" with aws_s3_bucket_public_access_block all-true, aws_s3_bucket_server_side_encryption_configuration AES256, aws_s3_bucket_versioning enabled
      - aws_s3_bucket_lifecycle_configuration: rule "delete-orphans" expires objects under uploads/staging/ after 1 day; rule "intelligent-tiering" transitions photos/ to STANDARD_IA after 30 days
      - Module output: bucket_name, bucket_arn
    notes: ""

  - id: 12.2
    title: S3 Gateway VPC Endpoint
    agent: backenddeveloper
    done: false
    depends_on: [12.1]
    acceptance_criteria:
      - aws_vpc_endpoint of service "com.amazonaws.${region}.s3" with vpc_endpoint_type="Gateway" is associated with the private subnet route tables from phase 1
      - Endpoint policy restricts access to the knotify-uploads bucket ARN(s) (and the Terraform state bucket if same account)
      - A Lambda integration test (using the existing shared layer) lists the uploads bucket from inside the VPC private subnet and confirms no NAT or public route was used
    notes: ""

  - id: 12.3
    title: knotify-uploads Lambda with pre-signed URL generation
    agent: backenddeveloper
    done: false
    depends_on: [12.1]
    acceptance_criteria:
      - src/functions/uploads/ implements POST /v1/profile/verification-docs and POST /v1/profile/photos; each accepts {contentType, fileName} and returns {uploadUrl, objectKey, expiresAt}
      - The S3 PUT pre-signed URL has expiresIn=300 seconds, condition on Content-Length-Range (max 10 MB), and a server-side-encryption header requirement
      - The objectKey path is "users/${user_id}/photos/${uuid}.jpg" for photos and "users/${user_id}/verification/${uuid}.${ext}" for docs; user_id is derived from JWT sub, never accepted as input
      - Integration test: signed-in user requests a pre-signed URL, the test uses the URL to PUT a small JPEG, the object appears in the bucket at the expected key
      - Integration test: an attempt to PUT a 20 MB file is rejected by S3's Content-Length-Range enforcement
    notes: ""

  - id: 12.4
    title: HTTP API routes for upload endpoints
    agent: backenddeveloper
    done: false
    depends_on: [12.3]
    acceptance_criteria:
      - POST /v1/profile/photos and POST /v1/profile/verification-docs registered as aws_apigatewayv2_route with Cognito JWT authorizer and integration pointing at knotify-uploads
      - terraform plan in dev shows the routes added
    notes: ""

  - id: 12.5
    title: CloudFront distribution for photo reads with signed URLs
    agent: backenddeveloper
    done: false
    depends_on: [12.1]
    acceptance_criteria:
      - A second aws_cloudfront_distribution (separate from the API edge distribution in phase 5) serves the photos bucket as origin via an OAC (Origin Access Control); the bucket policy grants only this OAC
      - aws_cloudfront_public_key + aws_cloudfront_key_group are provisioned for signed-URL verification; the corresponding private key is stored in Secrets Manager
      - A helper module exposes a Lambda-callable function to sign a CloudFront URL for a given key with a 1-hour expiry
      - Integration test: a Lambda generates a signed URL, the test fetches it and receives the object; the same URL after 1h returns 403
    notes: ""

  - id: 12.6
    title: Profile photo URL update flow
    agent: backenddeveloper
    done: false
    depends_on: [12.3, 12.5]
    acceptance_criteria:
      - The knotify-profile PATCH /v1/profile/me handler from phase 6 story 6.1 is extended so the photo_url field, when provided, is validated to be a key the calling user owns (path starts with users/<user_id>/photos/) and is rewritten to a signed CloudFront URL before being persisted to users.photo_url
      - The chosen_profile_avatar field follows the same rule
      - Integration test: user uploads a photo via 12.3, PATCHes the profile with the returned objectKey, then GET /v1/profile/me returns photo_url as a signed CloudFront URL
      - Integration test: user attempts PATCH with another user's objectKey (users/<other-uid>/...) and receives HTTP 403
    notes: ""

  - id: 12.7
    title: End-to-end upload + read test
    agent: backenddeveloper
    done: false
    depends_on: [12.3, 12.4, 12.5, 12.6]
    acceptance_criteria:
      - tests/integration/uploads_e2e_test.py signs up a user, requests a pre-signed URL via POST /v1/profile/photos, PUTs a JPEG to S3, PATCHes /v1/profile/me with the returned objectKey, GETs /v1/profile/me, fetches the signed CloudFront URL and verifies the bytes match the original upload
      - A separate test signs up a second user and attempts to PATCH using the first user's objectKey — the request is rejected with HTTP 403
    notes: ""
