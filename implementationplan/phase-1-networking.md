phase: 1
title: Networking foundations
last_updated: 2026-05-21

context_summary: |
  Establishes the per-environment VPC, subnets, route tables, and security groups that all subsequent phases consume. Implements §6 of architecture.md verbatim: VPC 10.0.0.0/16 with public (10.0.1.0/24, 10.0.2.0/24), private (10.0.11.0/24, 10.0.12.0/24), and DB (10.0.21.0/24, 10.0.22.0/24) subnets across two AZs, security groups sg-lambda and sg-aurora with Lambda→Aurora 5432 the only allowed flow, and no NAT Gateway (Lambdas have no internet egress in v1). Subsequent phases (Aurora, Lambdas, AppSync) attach to these networking primitives.

stories:
  - id: 1.1
    title: Networking Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/networking/main.tf declares an aws_vpc with cidr_block=10.0.0.0/16 and enable_dns_hostnames=true
      - Two public subnets (10.0.1.0/24, 10.0.2.0/24) and two private subnets (10.0.11.0/24, 10.0.12.0/24) and two DB subnets (10.0.21.0/24, 10.0.22.0/24) are created across distinct AZs (data.aws_availability_zones.available)
      - An aws_db_subnet_group spans both DB subnets and is exposed as an output named "db_subnet_group_name"
      - No aws_nat_gateway, no aws_internet_gateway route from private subnets, no eip
      - Module outputs vpc_id, private_subnet_ids (list), db_subnet_group_name
    notes: ""

  - id: 1.2
    title: Security groups for Lambda and Aurora
    agent: backenddeveloper
    done: false
    depends_on: [1.1]
    acceptance_criteria:
      - Module creates aws_security_group "sg-lambda" inside the VPC with zero inbound rules and an outbound rule allowing all traffic (placeholder; tightened later)
      - Module creates aws_security_group "sg-aurora" with a single inbound rule allowing TCP 5432 from sg-lambda's security_group_id only, and zero outbound rules
      - Module outputs lambda_security_group_id and aurora_security_group_id
      - terraform validate passes
    notes: ""

  - id: 1.3
    title: Per-environment instantiation
    agent: backenddeveloper
    done: false
    depends_on: [1.1, 1.2]
    acceptance_criteria:
      - File infrastructure/environments/dev/main.tf instantiates module.networking with appropriate inputs
      - File infrastructure/environments/prod/main.tf instantiates module.networking with appropriate inputs
      - File infrastructure/environments/dev/backend.tf points at the dev Terraform state bucket and lock table from phase 0
      - File infrastructure/environments/prod/backend.tf points at the prod equivalents
      - terraform init then terraform plan succeeds in both environments without surfacing destructive diffs against the smoke-test state
    notes: ""

  - id: 1.4
    title: Terraform tests for networking module
    agent: backenddeveloper
    done: false
    depends_on: [1.1, 1.2]
    acceptance_criteria:
      - File infrastructure/modules/networking/tests/networking.tftest.hcl exists with at least three test cases
      - One test asserts six subnets are created with the expected CIDR blocks
      - One test asserts sg-aurora has exactly one ingress rule and that the source security group id equals the sg-lambda id
      - One test asserts no aws_nat_gateway, no aws_internet_gateway, and no public route from private subnets
      - "terraform test" exits zero from infrastructure/modules/networking/
    notes: ""
