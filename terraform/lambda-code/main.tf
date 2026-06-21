################################################################################
# terraform/lambda-code/main.tf
# ---------------------------------
# Applied by the GitHub Actions deploy pipeline via the OIDC role from
# terraform/bootstrap/. This is the ONLY root the pipeline can apply —
# it can create/update the Lambda function and its EventBridge trigger,
# but has NO permissions to touch IAM policies, SNS topics, or the
# analysis cache bucket (those live in terraform/lambda-infra/, applied
# only by a human). See bootstrap/main.tf's deploy role policy for the
# exact, deliberately narrow permission set this implies.
#
# HARD REQUIREMENT: terraform/lambda-infra/ must be applied BEFORE this
# root. The data sources below look up lambda-infra's role, SNS topic,
# and bucket by name — if lambda-infra hasn't been applied yet, these
# lookups fail clearly at `plan` time.
#
# Sections:
#   1. Data sources         — lambda-infra's resources, looked up by name
#   2. Lambda build         — packages source + dependencies into a zip
#   3. Lambda function      — the actual deployed code
#   4. EventBridge          — scheduled trigger
################################################################################


################################################################################
# 1. DATA SOURCES
# -----------------
# Read-only lookups into resources created by terraform/lambda-infra/.
# Same safe pattern already used between bootstrap and lambda-infra —
# this root can READ these resources to wire up the function, but has
# no permission to create, modify, or delete any of them.
################################################################################

data "aws_iam_role" "lambda_execution" {
  name = "${var.project_name}-lambda-role"
}

data "aws_sns_topic" "drift_findings" {
  name = "${var.project_name}-drift-findings"
}

data "aws_caller_identity" "current" {}

# Note: aws_s3_bucket's lookup argument is `bucket`, not `name` — S3
# buckets don't have a separate name field distinct from their bucket
# name, unlike IAM roles or SNS topics.
data "aws_s3_bucket" "analysis_cache" {
  bucket = "${var.project_name}-analysis-cache-${data.aws_caller_identity.current.account_id}"
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
# 2. LAMBDA BUILD
# -----------------
# Unchanged from the original combined root — see
# BUILD-MECHANICS-NOTES.md for the full explanation of the source-hash
# fingerprinting and why sort() matters.
################################################################################

locals {
  lambda_source_files = sort(tolist(fileset("${path.module}/../../lambda", "**/*.py")))

  lambda_source_hash = sha256(join("", [
    for f in local.lambda_source_files :
    filesha256("${path.module}/../../lambda/${f}")
  ]))
}

resource "null_resource" "build_lambda" {
  triggers = {
    source_hash       = local.lambda_source_hash
    requirements_hash = filemd5("${path.module}/../../lambda/requirements.txt")

    # ALWAYS forces a rebuild, on every single apply, regardless of
    # whether source_hash/requirements_hash changed. This is the only
    # root in the project where that's correct: this root is applied by
    # a FRESH, EMPTY GitHub Actions runner every time, never the same
    # machine twice. State (now correctly remote, in S3) can say "the
    # build already happened" — but that was only ever true on whatever
    # machine actually ran build.sh. A new runner has no dist/package
    # folder regardless of what the trigger hashes say, so skipping the
    # rebuild here means archive_file has nothing to zip. The hash-based
    # skip logic genuinely only makes sense for a human re-applying
    # repeatedly from the same machine — which never happens for this
    # particular root.
    always_run = timestamp()
  }

  provisioner "local-exec" {
    command = "bash ${abspath(path.module)}/${var.build_script}"
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../dist/package"
  output_path = "${path.module}/../../dist/lambda.zip"

  depends_on = [null_resource.build_lambda]
}


################################################################################
# 3. LAMBDA FUNCTION
# ---------------------
# The role comes from a data source lookup now, not a same-root
# resource reference — this root can attach the function to
# lambda-infra's role, but can never modify that role itself.
################################################################################

resource "aws_lambda_function" "drift_monitor" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = var.project_name
  role             = data.aws_iam_role.lambda_execution.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = var.lambda_timeout_seconds
  memory_size = var.lambda_memory_mb

  environment {
    variables = {
      SNS_TOPIC_ARN         = data.aws_sns_topic.drift_findings.arn
      ANALYSIS_CACHE_BUCKET = data.aws_s3_bucket.analysis_cache.bucket

      ANTHROPIC_SSM_PARAMETER       = data.aws_ssm_parameter.anthropic_api_key.name
      GITHUB_TOKEN_SSM_PARAMETER    = data.aws_ssm_parameter.github_token.name
      TERRAFORM_REPOS_SSM_PARAMETER = data.aws_ssm_parameter.terraform_repos.name

      AI_MODEL                       = var.ai_model
      AI_EFFORT                      = var.ai_effort
      AI_ANALYSIS_SEVERITY_THRESHOLD = var.ai_analysis_severity_threshold
      AI_CHANGELOG_CACHE_TTL_HOURS   = tostring(var.ai_changelog_cache_ttl_hours)
      AI_TIME_BUDGET_SECONDS         = tostring(var.ai_time_budget_seconds)
    }
  }

  tags = {
    Project = var.project_name
  }
}


################################################################################
# 4. EVENTBRIDGE — SCHEDULED TRIGGER
# --------------------------------------
# Kept together with the function (rather than in lambda-infra) because
# the target and permission both genuinely depend on the function
# existing — EventBridge's PutTargets call requires a real function
# ARN, and AddPermission is a Lambda-specific API that requires the
# function to already exist. Splitting these from the function itself
# would recreate the exact cross-root dependency problem this split was
# designed to avoid.
################################################################################

resource "aws_cloudwatch_event_rule" "drift_monitor_schedule" {
  name                = "${var.project_name}-schedule"
  description         = "Triggers the Terraform version check on a schedule"
  schedule_expression = var.schedule_expression

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_event_target" "drift_monitor" {
  rule      = aws_cloudwatch_event_rule.drift_monitor_schedule.name
  target_id = "DriftMonitorLambda"
  arn       = aws_lambda_function.drift_monitor.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.drift_monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.drift_monitor_schedule.arn
}
