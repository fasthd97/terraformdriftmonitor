################################################################################
# terraform/lambda-infra/main.tf
# ----------------------------------
# Applied by a HUMAN, manually, from the same machine each time — same
# pattern as terraform/bootstrap/. Local state is fine here because,
# unlike terraform/lambda-code/, this root is never applied by an
# ephemeral CI/CD runner.
#
# Split from a single combined "terraform/lambda/" root specifically so
# the CI/CD pipeline's deploy role never needs permissions to manage
# IAM policies, SNS topics, or CloudWatch alarms — it can only touch
# what lives in terraform/lambda-code/ (the function, its build, and
# the EventBridge trigger). This mirrors the same blast-radius reasoning
# already used to separate terraform/bootstrap/ from this root.
#
# HARD REQUIREMENT: terraform/bootstrap/ must be applied BEFORE this
# root. This root reads bootstrap's SSM parameters via
# `data "aws_ssm_parameter"` lookups — if bootstrap hasn't been applied
# yet, `terraform plan` here fails immediately with a clear error.
#
# Sections:
#   1. Data sources       — account ID + bootstrap's SSM parameters
#   2. Analysis cache S3  — caches changelog analysis (immutable data)
#   3. SNS                — drift-finding notifications
#   4. Lambda execution role + permissions
#   5. CloudWatch log group
#   6. Self-monitoring alarms
################################################################################


################################################################################
# 1. DATA SOURCES
# -----------------
# The aws_ssm_parameter lookups here are READ-ONLY references to
# parameters created by terraform/bootstrap/. This root never creates
# or modifies them — it only needs their ARNs to grant its own Lambda
# role read access.
################################################################################

data "aws_caller_identity" "current" {}

# Resolves the actual KMS key ARN behind SSM's AWS-managed key alias.
# Used to scope kms:Decrypt precisely instead of wildcarding it.
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

data "aws_ssm_parameter" "anthropic_api_key" {
  name = "/${var.project_name}/anthropic-api-key"
}

data "aws_ssm_parameter" "github_token" {
  name = "/${var.project_name}/github-token"
}

data "aws_ssm_parameter" "terraform_repos" {
  name = "/${var.project_name}/terraform-repos"
}


################################################################################
# 2. ANALYSIS CACHE BUCKET
# ---------------------------
# Caches AI changelog extraction results. See lambda/checks/ai_changelog.py
# for the full caching design — default TTL is 90 days, with 0 meaning
# indefinite, since a specific provider version's release notes are an
# immutable historical record.
#
# Deliberately a SEPARATE bucket from bootstrap's deployments bucket —
# different purpose (analysis cache vs. deployment artifacts), different
# access pattern (the Lambda's own role vs. the GitHub deploy role).
################################################################################

resource "aws_s3_bucket" "analysis_cache" {
  # Account ID alone guarantees global uniqueness. Unlike bootstrap's
  # deployments bucket, this bucket only ever holds cached summaries of
  # PUBLIC Terraform provider changelogs — there's no sensitive data
  # here to protect via an unpredictable name, so we don't add that
  # complexity (or the extra `random` provider dependency) for no real
  # security benefit.
  bucket = "${var.project_name}-analysis-cache-${data.aws_caller_identity.current.account_id}"

  tags = {
    Project = var.project_name
    Purpose = "Caches AI changelog breaking-change extraction results"
  }
}

resource "aws_s3_bucket_public_access_block" "analysis_cache" {
  bucket = aws_s3_bucket.analysis_cache.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analysis_cache" {
  bucket = aws_s3_bucket.analysis_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

################################################################################
# NOTE ON LIFECYCLE: deliberately NO S3 lifecycle/expiration rule here.
#
# Two reasons, both specific to this bucket's actual access pattern:
#
# 1. It would directly contradict the indefinite caching option
#    (ai_changelog_cache_ttl_hours = 0) supported in ai_changelog.py.
#    A fixed-days S3 expiration would silently delete cache entries a
#    user explicitly chose to keep forever, causing a redundant AI call
#    the next time that entry is read — defeating the entire point of
#    choosing "indefinite" in the first place.
#
# 2. There's no real unbounded-growth risk to protect against here.
#    The cache key is provider+version (e.g. "hashicorp-aws-6.0.0.json").
#    That keyspace is naturally bounded — refreshing a stale entry
#    overwrites the SAME key, it doesn't create a new object.
################################################################################


################################################################################
# 3. SNS — DRIFT FINDING NOTIFICATIONS
# ---------------------------------------
# Deliberately separate from bootstrap's pipeline_alerts topic. That
# topic is for CI CD pipeline operational notifications (deploy
# success/failure, the AURORA cover message). This topic is for the
# actual drift-monitoring TOOL's findings — a different, ongoing
# concern with a potentially different audience.
################################################################################

resource "aws_sns_topic" "drift_findings" {
  name = "${var.project_name}-drift-findings"

  tags = {
    Project = var.project_name
    Purpose = "Terraform provider version drift findings"
  }
}

resource "aws_sns_topic_subscription" "drift_findings_email" {
  topic_arn = aws_sns_topic.drift_findings.arn
  protocol  = "email"
  endpoint  = var.alert_email
}


################################################################################
# 4. LAMBDA EXECUTION ROLE
# ---------------------------
# This is a DIFFERENT role from the GitHub Actions deploy role in
# bootstrap/. The deploy role's job is to UPLOAD code. This role's job
# is to RUN that code. Different identity, different trust relationship
# (Lambda service, not GitHub OIDC), different permissions entirely.
#
# Looked up by terraform/lambda-code/ via a data source (by name) so
# that root can attach the function to this role without ever being
# able to modify the role itself.
################################################################################

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "drift_monitor" {
  name               = "${var.project_name}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json

  tags = {
    Project = var.project_name
  }
}

# --- CloudWatch Logs ---
data "aws_iam_policy_document" "cloudwatch_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_name}:*"
    ]
  }
}

resource "aws_iam_policy" "cloudwatch_logs" {
  name   = "${var.project_name}-lambda-cloudwatch-logs"
  policy = data.aws_iam_policy_document.cloudwatch_logs.json
}

resource "aws_iam_role_policy_attachment" "cloudwatch_logs" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.cloudwatch_logs.arn
}

# --- Analysis Cache S3 Access ---
data "aws_iam_policy_document" "analysis_cache_access" {
  statement {
    sid    = "AnalysisCacheAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.analysis_cache.arn,
      "${aws_s3_bucket.analysis_cache.arn}/*",
    ]
  }
}

resource "aws_iam_policy" "analysis_cache_access" {
  name   = "${var.project_name}-lambda-analysis-cache"
  policy = data.aws_iam_policy_document.analysis_cache_access.json
}

resource "aws_iam_role_policy_attachment" "analysis_cache_access" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.analysis_cache_access.arn
}

# --- SSM Read (bootstrap-created parameters, read-only) ---
data "aws_iam_policy_document" "ssm_read" {
  statement {
    sid     = "ReadBootstrapSecrets"
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    resources = [
      data.aws_ssm_parameter.anthropic_api_key.arn,
      data.aws_ssm_parameter.github_token.arn,
      data.aws_ssm_parameter.terraform_repos.arn,
    ]
  }

  statement {
    sid       = "DecryptSecureStrings"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm.target_key_arn]
  }
}

resource "aws_iam_policy" "ssm_read" {
  name   = "${var.project_name}-lambda-ssm-read"
  policy = data.aws_iam_policy_document.ssm_read.json
}

resource "aws_iam_role_policy_attachment" "ssm_read" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.ssm_read.arn
}

# --- SNS Publish (drift findings topic only) ---
data "aws_iam_policy_document" "sns_publish" {
  statement {
    sid       = "PublishDriftFindingsOnly"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.drift_findings.arn]
  }
}

resource "aws_iam_policy" "sns_publish" {
  name   = "${var.project_name}-lambda-sns-publish"
  policy = data.aws_iam_policy_document.sns_publish.json
}

resource "aws_iam_role_policy_attachment" "sns_publish" {
  role       = aws_iam_role.drift_monitor.name
  policy_arn = aws_iam_policy.sns_publish.arn
}


################################################################################
# 5. CLOUDWATCH LOG GROUP
# --------------------------
# Explicit creation (rather than letting Lambda auto-create it) for
# control over retention — without this, logs accumulate forever.
################################################################################

resource "aws_cloudwatch_log_group" "drift_monitor" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days

  tags = {
    Project = var.project_name
  }
}


################################################################################
# 6. SELF-MONITORING ALARMS
# ----------------------------
# Watches the Lambda's own operational health. Publishes to
# drift_findings — the same topic the Lambda's own findings use.
#
# IMPORTANT — function reference fix from the split:
# These alarms previously referenced aws_lambda_function.drift_monitor
# directly (a same-root resource, before the split). Now that the
# function lives in terraform/lambda-code/, that reference no longer
# exists in THIS root. Using var.project_name directly instead is safe
# — the function's name is ALWAYS exactly var.project_name by
# construction (see terraform/lambda-code/main.tf), so this loses no
# correctness, it just removes a cross-root dependency that can't exist.
#
# Note on the "not invoked" alarm period: CloudWatch rejects any single
# period >= 3600 seconds combined with evaluation_periods such that
# period * evaluation_periods exceeds 7 days (604800 seconds). Using
# 1-day periods x 7 evaluations gets the same "hasn't run in a week"
# check within that limit.
################################################################################

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-errors"
  alarm_description   = "Drift monitor Lambda threw an uncaught exception. Check CloudWatch logs."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = var.project_name
  }

  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.drift_findings.arn]
  ok_actions    = [aws_sns_topic.drift_findings.arn]

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.project_name}-throttles"
  alarm_description   = "Drift monitor Lambda was throttled by AWS."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions = {
    FunctionName = var.project_name
  }

  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.drift_findings.arn]
  ok_actions    = [aws_sns_topic.drift_findings.arn]

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_not_invoked" {
  alarm_name          = "${var.project_name}-not-invoked"
  alarm_description   = "Drift monitor Lambda has not run in 7 days. EventBridge may have stopped firing."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions = {
    FunctionName = var.project_name
  }

  statistic           = "Sum"
  period              = 86400 # 1 day
  evaluation_periods  = 7
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # No data across all 7 evaluation periods means the Lambda never
  # ran — treat that as a breach, not as "insufficient data."
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.drift_findings.arn]

  tags = {
    Project = var.project_name
  }
}
