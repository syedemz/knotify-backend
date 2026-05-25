terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.20"
    }
  }
}

# ---------------------------------------------------------------------------
# Observability Lambda layer
#
# Packages aws-lambda-powertools, PyJWT[crypto], requests, and the
# knotify_obs wrapper module.  The zip artifact is produced by running:
#
#   bash infrastructure/src/layers/observability/build.sh
#
# from the repository root before `terraform apply`.
# ---------------------------------------------------------------------------

resource "aws_lambda_layer_version" "observability" {
  layer_name  = "knotify-${var.environment}-observability"
  description = "aws-lambda-powertools 3.29.0, PyJWT[crypto] 2.13.0, knotify_obs wrapper"

  filename         = var.zip_path
  source_code_hash = filebase64sha256(var.zip_path)

  compatible_runtimes      = ["python3.14"]
  compatible_architectures = ["arm64"]
}
