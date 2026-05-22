# PROD BACKEND — NOT YET EXERCISED.
# The knotify-prod-tfstate S3 bucket has not been provisioned.
# Do NOT run terraform init/plan/apply against this backend until
# the cutover checklist in docs/PROD_CUTOVER.md is complete.
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
  # Single state file for the entire prod environment across phases 1–11.
  # Phases 2–11 add module blocks to main.tf and write to this same key.
  # Do NOT change this key.
  backend "s3" {
    bucket         = "knotify-prod-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "eu-central-1"
    dynamodb_table = "knotify-tfstate-lock"
    encrypt        = true
  }
}
