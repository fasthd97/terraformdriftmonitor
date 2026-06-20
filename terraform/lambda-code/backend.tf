################################################################################
# terraform/lambda-code/backend.tf
# ------------------------------------
# PARTIAL backend configuration, deliberately. Terraform backend blocks
# cannot use variables or interpolation — values must be literal. The
# state bucket's name is unpredictable by design (see bootstrap's
# lambda_state bucket comment), so it can't be hardcoded here without
# defeating that design.
#
# Instead: only the static parts live in this file. `bucket` and
# `region` are supplied at `terraform init` time via -backend-config
# flags, reading from the STATE_BUCKET and AWS_REGION values that are
# already GitHub repo variables (same pattern as AWS_ROLE_ARN and
# DEPLOYMENTS_BUCKET).
#
# Required every time this root is initialized, locally or in CI:
#
#   terraform init \
#     -backend-config="bucket=<STATE_BUCKET value>" \
#     -backend-config="region=<AWS_REGION value>"
#
# use_lockfile = true enables Terraform's native S3 state locking
# (generally available since Terraform 1.11) — no DynamoDB table
# needed. This is the ONLY root in this project that needs a remote
# backend at all, because it's the only one applied by an ephemeral
# CI/CD runner rather than a human on the same machine every time.
################################################################################

terraform {
  backend "s3" {
    key          = "lambda-code/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}
