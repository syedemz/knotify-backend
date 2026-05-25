terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      # python3.14 runtime requires provider >= 6.20.0 (story 3.1 notes).
      version = "~> 6.20"
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
