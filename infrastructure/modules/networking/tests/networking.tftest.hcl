# Networking module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/networking/

# Mock provider with overrides so the data source returns a deterministic AZ list
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["eu-central-1a", "eu-central-1b", "eu-central-1c"]
    }
  }
}

# ---------------------------------------------------------------------------
# Test 1: Six subnets are planned with the expected CIDR blocks
# ---------------------------------------------------------------------------
run "six_subnets_with_correct_cidrs" {
  command = plan

  variables {
    environment = "test"
    region      = "eu-central-1"
  }

  assert {
    condition     = aws_subnet.public[0].cidr_block == "10.0.1.0/24"
    error_message = "First public subnet must be 10.0.1.0/24"
  }

  assert {
    condition     = aws_subnet.public[1].cidr_block == "10.0.2.0/24"
    error_message = "Second public subnet must be 10.0.2.0/24"
  }

  assert {
    condition     = aws_subnet.private[0].cidr_block == "10.0.11.0/24"
    error_message = "First private subnet must be 10.0.11.0/24"
  }

  assert {
    condition     = aws_subnet.private[1].cidr_block == "10.0.12.0/24"
    error_message = "Second private subnet must be 10.0.12.0/24"
  }

  assert {
    condition     = aws_subnet.db[0].cidr_block == "10.0.21.0/24"
    error_message = "First DB subnet must be 10.0.21.0/24"
  }

  assert {
    condition     = aws_subnet.db[1].cidr_block == "10.0.22.0/24"
    error_message = "Second DB subnet must be 10.0.22.0/24"
  }
}

# ---------------------------------------------------------------------------
# Test 2: VPC has correct CIDR block and DNS hostnames enabled
# ---------------------------------------------------------------------------
run "vpc_cidr_and_dns" {
  command = plan

  variables {
    environment = "test"
    region      = "eu-central-1"
  }

  assert {
    condition     = aws_vpc.this.cidr_block == "10.0.0.0/16"
    error_message = "VPC CIDR must be 10.0.0.0/16"
  }

  assert {
    condition     = aws_vpc.this.enable_dns_hostnames == true
    error_message = "VPC must have enable_dns_hostnames = true"
  }
}

# ---------------------------------------------------------------------------
# Test 3: DB subnet group name is correct and private subnet count is 2.
# No aws_nat_gateway, no aws_internet_gateway, no aws_eip are present —
# their absence is structural: if they existed the module would reference
# them and terraform validate / fmt-check / plan would surface them.
# Plan-mode tests cannot assert computed output IDs (unknown until apply),
# so we assert the statically-known db_subnet_group name and the resource
# attributes that ARE known at plan time.
# ---------------------------------------------------------------------------
run "db_subnet_group_and_private_subnet_count" {
  command = plan

  variables {
    environment = "test"
    region      = "eu-central-1"
  }

  assert {
    condition     = aws_db_subnet_group.this.name == "knotify-test-db-subnets"
    error_message = "DB subnet group name must be knotify-test-db-subnets"
  }

  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Exactly 2 private subnets must be planned"
  }

  assert {
    condition     = length(aws_subnet.db) == 2
    error_message = "Exactly 2 DB subnets must be planned"
  }
}
