terraform {
  required_version = ">= 1.7"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
}

resource "random_id" "suffix" {
  byte_length = 4
}

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
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
