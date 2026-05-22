phase: 10
title: Observability consolidation
last_updated: 2026-05-21

context_summary: |
  Per §13 #18 resolution in architecture.md v1.6 (hybrid Option C, owner picked dedicated phase Option A): individual Lambda functions already ship with Powertools structured logging, correlation IDs, and metric emission from phase 3 onward. This phase consolidates the alarms, dashboards, and routing on top. All CloudWatch log groups must already use 7-day retention per the owner's cost-control directive (audited here). One CloudWatch dashboard per environment surfaces the operational signals. SNS topic per environment delivers alarms to the owner email. No third-party tools (Sentry, Datadog) are added — CloudWatch only.

stories:
  - id: 10.1
    title: SNS topic and email subscription for alarms
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/alarms/main.tf creates aws_sns_topic "knotify-alarms" per environment with KMS encryption (default key) and an aws_sns_topic_subscription with protocol=email and endpoint sourced from var.alarm_email
      - Module output: alarm_topic_arn
      - terraform plan in dev shows the topic and subscription created; the email recipient receives the confirm-subscription request when applied
    notes: ""

  - id: 10.2
    title: Per-Lambda alarms (errors, throttles, duration)
    agent: backenddeveloper
    done: false
    depends_on: [10.1]
    acceptance_criteria:
      - The alarms module iterates over a variable lambda_function_names and creates three aws_cloudwatch_metric_alarm resources per function: Errors > 1% of Invocations over 5 minutes, Throttles > 0 over 5 minutes, Duration p99 > 80% of timeout over 15 minutes
      - All alarms set alarm_actions to the SNS topic from 10.1 and treat_missing_data="notBreaching"
      - terraform validate passes; a unit test (.tftest.hcl) instantiates the module with two function names and asserts 6 alarms are produced
    notes: ""

  - id: 10.3
    title: Aurora alarms
    agent: backenddeveloper
    done: false
    depends_on: [10.1]
    acceptance_criteria:
      - Alarms: CPUUtilization > 80% for 10 min, DatabaseConnections > 80% of max for 5 min, DeadlockCount > 0 for 5 min, ServerlessDatabaseCapacity at max ACU for 10 min
      - alarm_actions wired to the SNS topic
    notes: ""

  - id: 10.4
    title: DynamoDB alarms
    agent: backenddeveloper
    done: false
    depends_on: [10.1]
    acceptance_criteria:
      - For each table (ChatRooms, ChatRoomMembership, ChatMessages, MessageReads, Notifications, PushNotificationTokens, account_deletion_audit): ReadThrottleEvents > 0 over 5 min, WriteThrottleEvents > 0 over 5 min, SystemErrors > 0 over 5 min
      - alarm_actions wired to the SNS topic
    notes: ""

  - id: 10.5
    title: API Gateway alarms
    agent: backenddeveloper
    done: false
    depends_on: [10.1]
    acceptance_criteria:
      - Alarms: 5XXError rate > 1% over 5 min, 4XXError rate > 5% over 15 min (excluding 401/403 paths via metric filter), Latency p99 > 1500 ms over 15 min
      - alarm_actions wired to the SNS topic
    notes: ""

  - id: 10.6
    title: Step Functions and AppSync alarms
    agent: backenddeveloper
    done: false
    depends_on: [10.1]
    acceptance_criteria:
      - aws_cloudwatch_metric_alarm on Step Functions ExecutionsFailed > 0 over 5 min (per state machine ARN from phase 9)
      - AppSync 5xxError rate > 0.1% and 4xxError rate > 2% per API id from phase 8
      - alarm_actions wired to the SNS topic
    notes: ""

  - id: 10.7
    title: CloudWatch dashboard per environment
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/dashboard/main.tf creates aws_cloudwatch_dashboard "knotify-${var.environment}" with widgets for: Lambda invocations and errors by function, Aurora CPU + connections, DynamoDB consumed RCU/WCU per table, API Gateway request count and 5xx rate, Step Functions executions started/succeeded/failed, AppSync request count
      - Dashboard JSON is rendered from a Terraform template; terraform apply in dev creates a dashboard visible in the AWS console with all widgets populating data
    notes: ""

  - id: 10.8
    title: Log retention audit
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - A new Terraform test (under infrastructure/tests/log_retention.tftest.hcl) asserts that every aws_cloudwatch_log_group resource defined in any module has retention_in_days exactly 7
      - A one-time audit script tools/audit_log_retention.py uses boto3 to list every /aws/lambda/*, /aws/apigateway/*, /aws/states/*, and /aws/appsync/apis/* log group in both AWS accounts and prints any whose retention != 7; the script exits zero only when every group matches
      - Running the script against both dev and prod exits zero
    notes: ""

  - id: 10.9
    title: Operational runbook
    agent: backenddeveloper
    done: false
    depends_on: [10.2, 10.3, 10.4, 10.5, 10.6]
    acceptance_criteria:
      - A file docs/runbook.md documents each alarm name, the metric and threshold, the most likely root cause, and the first three diagnostic steps (log query, dashboard panel, AWS console link)
      - At least one runbook entry exists for every alarm created in stories 10.2–10.6
    notes: ""
