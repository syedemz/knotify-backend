terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  # Deterministic slice of the first two AZs — never hardcode AZ names
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name        = "knotify-${var.environment}-vpc"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Subnets — public (reserved for future IGW/ALB, unused in v1)
# ---------------------------------------------------------------------------

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = ["10.0.1.0/24", "10.0.2.0/24"][count.index]
  availability_zone = element(local.azs, count.index)

  tags = {
    Name        = "knotify-${var.environment}-public-${element(local.azs, count.index)}"
    Environment = var.environment
    Tier        = "public"
  }
}

# ---------------------------------------------------------------------------
# Subnets — private (Lambda placement)
# ---------------------------------------------------------------------------

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = ["10.0.11.0/24", "10.0.12.0/24"][count.index]
  availability_zone = element(local.azs, count.index)

  tags = {
    Name        = "knotify-${var.environment}-private-${element(local.azs, count.index)}"
    Environment = var.environment
    Tier        = "private"
  }
}

# ---------------------------------------------------------------------------
# Subnets — DB (Aurora subnet group)
# ---------------------------------------------------------------------------

resource "aws_subnet" "db" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = ["10.0.21.0/24", "10.0.22.0/24"][count.index]
  availability_zone = element(local.azs, count.index)

  tags = {
    Name        = "knotify-${var.environment}-db-${element(local.azs, count.index)}"
    Environment = var.environment
    Tier        = "db"
  }
}

# ---------------------------------------------------------------------------
# Route tables
# One public route table (no routes — IGW not created in v1)
# One private route table per private subnet (architecture §6.3)
# One DB route table per DB subnet (no routes — no NAT)
# ---------------------------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name        = "knotify-${var.environment}-public-rt"
    Environment = var.environment
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.this.id

  tags = {
    Name        = "knotify-${var.environment}-private-rt-${element(local.azs, count.index)}"
    Environment = var.environment
  }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table" "db" {
  count  = 2
  vpc_id = aws_vpc.this.id

  tags = {
    Name        = "knotify-${var.environment}-db-rt-${element(local.azs, count.index)}"
    Environment = var.environment
  }
}

resource "aws_route_table_association" "db" {
  count          = 2
  subnet_id      = aws_subnet.db[count.index].id
  route_table_id = aws_route_table.db[count.index].id
}

# ---------------------------------------------------------------------------
# DB subnet group — spans both DB subnets for Aurora
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  name        = "knotify-${var.environment}-db-subnets"
  description = "Knotify ${var.environment} Aurora DB subnet group"
  subnet_ids  = aws_subnet.db[*].id

  tags = {
    Name        = "knotify-${var.environment}-db-subnet-group"
    Environment = var.environment
  }
}
