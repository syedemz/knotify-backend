provider "aws" {
  region = "eu-central-1"

  default_tags {
    tags = {
      Project     = "knotify"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "knotify-team"
    }
  }
}

module "networking" {
  source      = "../../modules/networking"
  environment = var.environment
  region      = "eu-central-1"
}
