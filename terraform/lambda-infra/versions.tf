################################################################################
# terraform/lambda-infra/versions.tf
# --------------------------------------
# Only the aws provider — this root doesn't build or zip anything
# (no archive/null providers needed, those moved to lambda-code/).
################################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
