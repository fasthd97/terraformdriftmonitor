################################################################################
# terraform/lambda-code/variables.tf
# --------------------------------------
# project_name MUST match terraform/bootstrap/ AND terraform/lambda-infra/
# exactly — this root looks up lambda-infra's resources by name.
#
# Deliberately does NOT include alert_email or log_retention_days —
# those belong to terraform/lambda-infra/, since that's where the SNS
# subscription and log group actually live.
################################################################################

variable "aws_region" {
  description = "AWS region to deploy into. Must match bootstrap and lambda-infra."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix for all resources. MUST match terraform/bootstrap/ and terraform/lambda-infra/ exactly — this root looks up lambda-infra's role, topic, and bucket by name."
  type        = string
  default     = "tfdriftmonitor"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.project_name))
    error_message = "project_name must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens."
  }
}

# ------------------------------------------------------------
# SCHEDULE
# ------------------------------------------------------------

variable "schedule_expression" {
  description = "EventBridge schedule expression for how often to run the version check"
  type        = string
  default     = "rate(7 days)"
}

# ------------------------------------------------------------
# AI MODEL CONFIGURATION
# ------------------------------------------------------------

variable "ai_model" {
  description = "Anthropic model ID used for changelog breaking-change extraction."
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "ai_effort" {
  description = "Anthropic API effort level. Only applies to models that support it (see MODELS_SUPPORTING_EFFORT in ai_changelog.py)."
  type        = string
  default     = "medium"

  validation {
    condition     = contains(["low", "medium", "high", "max"], var.ai_effort)
    error_message = "Must be one of: low, medium, high, max"
  }
}

variable "ai_analysis_severity_threshold" {
  description = "Minimum finding severity that triggers AI changelog analysis."
  type        = string
  default     = "CRITICAL"

  validation {
    condition     = contains(["CRITICAL", "WARNING", "INFO"], var.ai_analysis_severity_threshold)
    error_message = "Must be one of: CRITICAL, WARNING, INFO"
  }
}

variable "ai_changelog_cache_ttl_hours" {
  description = "How long (hours) to treat cached changelog analysis as valid. 0 = indefinite."
  type        = number
  default     = 2160 # 90 days
}

variable "ai_time_budget_seconds" {
  description = "Maximum wall-clock time per run spent on NEW (cache-miss) AI changelog calls before deferring remaining findings to the next run."
  type        = number
  default     = 180
}

# ------------------------------------------------------------
# LAMBDA RUNTIME
# ------------------------------------------------------------

variable "lambda_timeout_seconds" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB. Sized for network throughput (I/O-bound workload), not RAM capacity."
  type        = number
  default     = 256
}

# ------------------------------------------------------------
# BUILD SCRIPT
# ------------------------------------------------------------

variable "build_script" {
  description = "Path to the Lambda build script, relative to terraform/lambda-code/"
  type        = string
  default     = "../../scripts/build.sh"
}
