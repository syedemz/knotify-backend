phase: 5
title: API edge (HTTP API + CloudFront + WAF)
last_updated: 2026-05-21

context_summary: |
  Builds the inbound HTTPS surface per §4.2 of architecture.md: HTTP API Gateway behind CloudFront with WAF attached at the edge, a Cognito JWT authorizer wired to the User Pool from phase 4, custom domain backed by an ACM certificate, and an origin-secret header that prevents bypassing CloudFront. No domain Lambdas attach yet — that begins in phase 6. A stub "hello" Lambda is wired only as a smoke endpoint to validate that JWT enforcement, WAF rules, and the origin-secret check all function end-to-end through the edge stack.

stories:
  - id: 5.1
    title: HTTP API Gateway Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/api_gateway/main.tf creates aws_apigatewayv2_api with protocol_type="HTTP" and a default stage with throttling burst_limit and rate_limit configurable via variables (defaults 50/100 in dev, 500/1000 in prod)
      - aws_apigatewayv2_authorizer of type JWT references the Cognito User Pool issuer URL from phase 4 and audience equal to the app client id
      - Module outputs api_id, execute_api_endpoint, authorizer_id, default_stage_arn
    notes: ""

  - id: 5.2
    title: ACM certificate for custom domain in us-east-1
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/acm/main.tf creates an aws_acm_certificate in us-east-1 (provider alias "us_east_1") for the configured domain name (var.domain_name) with subject_alternative_names for any sub-domains
      - DNS validation records are output as a map suitable for consumption by a Route 53 module in story 5.5
      - Module accepts an optional variable to skip creation when domain ownership is not yet established in dev (so dev can use the default execute-api endpoint while prod uses the custom domain)
    notes: ""

  - id: 5.3
    title: CloudFront distribution with origin secret header
    agent: backenddeveloper
    done: false
    depends_on: [5.1, 5.2]
    acceptance_criteria:
      - aws_cloudfront_distribution created with the HTTP API execute_api_endpoint as the origin
      - A custom origin request policy injects the header "x-knotify-edge-secret" with a value sourced from a random_password resource (length 64) so the secret rotates only on explicit terraform taint
      - viewer_certificate uses the ACM cert from 5.2 with TLS 1.2 minimum
      - default_cache_behavior has caching disabled (forward all headers, query strings, cookies; min_ttl=0, default_ttl=0, max_ttl=0)
      - aliases includes the configured domain name when provided
      - Module outputs distribution_domain_name and edge_secret (sensitive)
    notes: ""

  - id: 5.4
    title: WAF web ACL attached to CloudFront
    agent: backenddeveloper
    done: false
    depends_on: [5.3]
    acceptance_criteria:
      - aws_wafv2_web_acl scope="CLOUDFRONT" (provider alias us_east_1) contains rules: AWSManagedRulesCommonRuleSet (priority 1), AWSManagedRulesKnownBadInputsRuleSet (priority 2), AWSManagedRulesSQLiRuleSet (priority 3), and a rate-based rule capped at 2000 requests per 5 minutes per IP (priority 10)
      - aws_wafv2_web_acl_association attaches the ACL to the CloudFront distribution from 5.3
      - cloudwatch_metrics_enabled and sampled_requests_enabled both true on the ACL
    notes: ""

  - id: 5.5
    title: Route 53 records for custom domain
    agent: backenddeveloper
    done: false
    depends_on: [5.2, 5.3]
    acceptance_criteria:
      - When var.domain_name is set, aws_route53_record entries point the configured domain at the CloudFront distribution_domain_name (A alias) in the appropriate hosted zone (id provided by variable)
      - DNS validation records for the ACM cert are also created in the same module
      - In dev, when var.domain_name is empty, no Route 53 resources are created and the edge stack is reachable via the CloudFront generated d*.cloudfront.net hostname
    notes: ""

  - id: 5.6
    title: HTTP API rejects requests missing the origin secret header
    agent: backenddeveloper
    done: false
    depends_on: [5.1, 5.3]
    acceptance_criteria:
      - A request authorizer Lambda or an API Gateway integration policy rejects any request whose x-knotify-edge-secret header does not match the value injected by CloudFront
      - An integration test that calls the HTTP API directly (bypassing CloudFront) with a valid Cognito JWT but no edge-secret header receives HTTP 403
      - An integration test through CloudFront with a valid Cognito JWT receives HTTP 200 against the stub endpoint from story 5.7
    notes: ""

  - id: 5.7
    title: Stub hello endpoint and end-to-end edge smoke test
    agent: backenddeveloper
    done: false
    depends_on: [5.1, 5.3, 5.4, 5.6]
    acceptance_criteria:
      - A throwaway src/functions/hello/ Lambda returns {"ok": true, "user_id": <sub from JWT>} and is wired to GET /v1/_internal/hello on the HTTP API behind the JWT authorizer
      - End-to-end integration test signs in a test user via Cognito (reusing the helper from phase 4 story 4.5), then GET <cloudfront-domain>/v1/_internal/hello with the Authorization header returns 200 and a user_id matching the test user's sub
      - The same endpoint without the Authorization header returns 401
      - A request including SQL injection patterns in the URL (e.g., /v1/_internal/hello?q=1 UNION SELECT) is rejected by WAF with 403 before reaching the Lambda
      - The /v1/_internal/hello route is removed from the codebase before phase 6 begins (a tracking note is added to phase 6's notes field)
    notes: ""
