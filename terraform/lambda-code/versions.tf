################################################################################
# terraform/lambda-code/versions.tf
# -------------------------------------
# required_version >= 1.11 specifically (not the project's usual >= 1.5)
# because native S3 state locking (use_lockfile, see backend.tf) only
# became generally available at 1.11. This fails clearly at `init` time
# on an outdated local Terraform install, rather than silently
# misbehaving — the documented safety net for this specific feature.
################################################################################

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
