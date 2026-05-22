terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
  # Single state file for the entire dev environment across phases 1–11.
  # Phases 2–11 add module blocks to main.tf and write to this same key.
  # Do NOT change this key.
  backend "s3" {
    bucket         = "knotify-dev-tfstate"
    key            = "dev/terraform.tfstate"
    region         = "eu-central-1"
    dynamodb_table = "knotify-tfstate-lock"
    encrypt        = true
  }
}
