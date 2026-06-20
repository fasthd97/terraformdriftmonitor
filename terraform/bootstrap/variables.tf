################################################################################
# terraform/bootstrap/variables.tf
# ----------------------------------
# All configurable inputs for the bootstrap infrastructure.
################################################################################

variable "aws_region" {
  description = "AWS region for all bootstrap resources. Kept region-agnostic via this variable."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix for all resources in this project"
  type        = string
  default     = "tfdriftmonitor"

  validation {
    # Same fix as terraform/lambda/variables.tf — restricted to the
    # safest common subset across IAM tags, S3 bucket names, SNS topic
    # names, and SSM parameter paths. Catches invalid characters at
    # `terraform plan` time rather than partway through a real apply.
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.project_name))
    error_message = "project_name must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "github_org" {
  description = "GitHub organisation or username that owns the repo"
  type        = string
  default     = "fasthd97"
}

variable "github_repo" {
  description = "GitHub repository name (without the org prefix)"
  type        = string
  default     = "terraformdriftmonitor"
}

variable "ses_alert_email" {
  description = "Email address for the out-of-band AURORA tamper alert (SES). Must be verified in SES before use."
  type        = string
}

variable "sns_alert_email" {
  description = "Email address for routine SNS pipeline notifications"
  type        = string
}
