################################################################################
# terraform/lambda-infra/variables.tf
# ---------------------------------------
# project_name MUST match terraform/bootstrap/ AND terraform/lambda-code/.
# This root constructs SSM/KMS ARNs by convention, same pattern as
# bootstrap and the original combined lambda root.
#
# Deliberately a SMALLER set than the original combined root — only
# variables this root's OWN resources actually use. Schedule, AI
# config, Lambda sizing, and the build script all moved to
# terraform/lambda-code/, since that's where the resources consuming
# them now live.
################################################################################

variable "aws_region" {
  description = "AWS region to deploy into. Must match the region bootstrap was applied to."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix for all resources. MUST match terraform/bootstrap/ and terraform/lambda-code/ exactly."
  type        = string
  default     = "tfdriftmonitor"

  validation {
    # Restricted to the safest common subset across IAM tags, S3
    # bucket names, SNS topic names, and SSM parameter paths. Catches
    # invalid characters at `terraform plan` time, before anything
    # touches AWS. See TROUBLESHOOTING.md's standing tag-value rule.
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.project_name))
    error_message = "project_name must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "alert_email" {
  description = "Email address for drift-finding SNS notifications (separate from the bootstrap pipeline_alerts topic)."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}
