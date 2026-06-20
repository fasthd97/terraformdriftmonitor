################################################################################
# terraform/bootstrap/versions.tf
# --------------------------------
# This is a SEPARATE Terraform root from terraform/lambda/.
# It is applied manually by a human, ONE TIME (or rarely, when bootstrap
# infrastructure needs to change). The GitHub Actions pipeline NEVER
# touches this root — that separation is itself a security control.
#
# Why separate:
#   - The OIDC role defined here is what GRANTS the pipeline its permissions.
#     If the pipeline could modify its own role, a compromise of the
#     pipeline could escalate its own access. Keeping this root out of
#     reach of CI/CD means the blast radius of a compromised pipeline
#     is limited to exactly what terraform/lambda/ can do.
################################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
