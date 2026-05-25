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
# DB Lambda layer
#
# Packages psycopg2-binary (manylinux2014_aarch64), pgvector Python client,
# and the knotify_db wrapper module (get_connection, set_rls_context,
# rls_context context manager).  The zip artifact is produced by running:
#
#   bash infrastructure/src/layers/db/build.sh
#
# from the repository root before `terraform apply`.
# ---------------------------------------------------------------------------

resource "aws_lambda_layer_version" "db" {
  layer_name  = "knotify-${var.environment}-db"
  description = "psycopg2-binary 2.9.12, pgvector 0.3.6, knotify_db wrapper"

  filename         = var.zip_path
  source_code_hash = filebase64sha256(var.zip_path)

  compatible_runtimes      = ["python3.14"]
  compatible_architectures = ["arm64"]
}
