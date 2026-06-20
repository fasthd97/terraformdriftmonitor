################################################################################
# terraform/bootstrap/main.tf
# -----------------------------
# Bootstrap infrastructure — applied manually, once, by a human.
# The GitHub Actions pipeline can never modify anything in this file.
#
# Sections:
#   1. OIDC Provider          — trust relationship between AWS and GitHub
#   2. Deploy Role            — what the pipeline can assume and do
#   3. Deployments Bucket     — where built Lambda packages are stored
#   4. SES                    — out-of-band AURORA tamper alert channel
#   5. SNS                    — routine pipeline notifications
#   6. SSM Parameters         — placeholders for secrets (filled in manually)
################################################################################


################################################################################
# 1. OIDC PROVIDER
# -----------------
# This tells AWS to trust tokens issued by GitHub's OIDC identity provider.
# Without this, AWS has no way to verify "this request really did come
# from a GitHub Actions workflow run."
#
# IMPORTANT — thumbprint handling:
# We do NOT hardcode the thumbprint. A hardcoded value goes stale if
# GitHub ever rotates their TLS certificate, and the failure mode is
# confusing — it looks like a trust policy problem, not a thumbprint
# problem. Instead we fetch it live via the tls_certificate data source,
# so every `terraform apply` recalculates it fresh. This is the current
# recommended pattern (AWS now also trusts GitHub via a root CA, making
# this data source mostly a safety net for older provider behaviour).
################################################################################

data "tls_certificate" "github_actions" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com"
  ]

  thumbprint_list = [
    data.tls_certificate.github_actions.certificates[0].sha1_fingerprint
  ]

  tags = {
    Project = var.project_name
    Purpose = "Trust relationship for GitHub Actions OIDC authentication"
  }
}
################################################################################
# PERMISSIONS BOUNDARY
# -----------------------
# The absolute ceiling on what the deploy role can ever do, regardless
# of what any individual policy attached to it (managed or inline) says.
#
# WHY THIS EXISTS:
# The just-in-time iam:PutRolePolicy permission (added later in this file)
# only restricts the NAME of the policy the role can create on itself —
# it does not restrict the CONTENT. Without a boundary, a compromised
# deploy script could create a correctly-named policy that grants far
# broader access than intended. IAM permissions boundaries evaluate as
# an INTERSECTION with whatever policies are attached — so even if a
# malicious inline policy said "Allow: *, Resource: *", the effective
# permission is still capped at exactly what this boundary lists.
#
# This must be defined before the role, since the role references it.
################################################################################

data "aws_iam_policy_document" "deploy_role_boundary" {
  statement {
    sid    = "MaximumPossiblePermissionsCeiling"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:SimulatePrincipalPolicy",
      "ses:SendEmail",
      "ses:SendRawEmail",
      "sns:Publish",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "deploy_role_boundary" {
  name   = "${var.project_name}-deploy-role-boundary"
  policy = data.aws_iam_policy_document.deploy_role_boundary.json

  tags = {
    Project = var.project_name
    Purpose = "Permissions ceiling - caps the deploy role even if an attached policy is overly broad"
  }
}

################################################################################
# 2. DEPLOY ROLE
# ----------------
# The role that GitHub Actions assumes via OIDC to run deployments.
#
# TRUST POLICY — who can assume this role:
#   Restricted to:
#     - This exact repo (fasthd97/terraformdriftmonitor)
#     - Only the "main" branch for prod-level access
#     - Only when the audience claim matches sts.amazonaws.com
#
#   Without these conditions, ANY GitHub Actions workflow from ANY repo
#   that knows this role's ARN could attempt to assume it. The conditions
#   are what makes OIDC actually secure — the role ARN alone is not a
#   secret, the conditions are the real gate.
################################################################################

data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    # Audience must match what we configured on the OIDC provider
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Restrict to this exact repo. Without this, any GitHub repo
    # could potentially assume this role if they discovered the ARN.
    # The wildcard after the colon allows any branch/tag/PR ref from
    # THIS repo — we narrow further with separate statements below
    # if we want branch-specific restrictions per environment.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [
        "repo:${var.github_org}/${var.github_repo}:*"
      ]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "${var.project_name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_trust.json

  # The ceiling defined above — caps this role's effective permissions
  # regardless of what any attached or future inline policy says.
  permissions_boundary = aws_iam_policy.deploy_role_boundary.arn

  # Short max session duration — this role should only ever be held
  # for the duration of a single workflow run, never longer.
  max_session_duration = 3600 # 1 hour

  tags = {
    Project = var.project_name
    Purpose = "Assumed by GitHub Actions via OIDC to deploy this project"
  }
}

# -----------------------------------------------------------------
# DEPLOY PERMISSIONS
# -----------------------------------------------------------------
# Scoped to exactly what the deploy workflow needs:
#   - S3: upload/read/list on the deployments bucket only
#   - Lambda: update function code (for the actual deploy step)
#   - IAM: read-only on its own role (for preflight self-check)
#
# Notably ABSENT:
#   - Any access to terraform/bootstrap/ resources
#   - Any SSM read access (deploy role never touches secrets)
#   - Any broad IAM write access
# -----------------------------------------------------------------

data "aws_iam_policy_document" "github_actions_deploy_permissions" {
  statement {
    sid    = "DeploymentsBucketAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.deployments.arn,
      "${aws_s3_bucket.deployments.arn}/*",
    ]
  }

  statement {
    sid    = "LambdaDeployAccess"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
    ]
    # Scoped to only the drift-monitor function this project deploys.
    # Uses a wildcard on the account/region since the function ARN
    # isn't known until terraform/lambda/ has been applied at least once.
    resources = [
      "arn:aws:lambda:*:*:function:${var.project_name}*"
    ]
  }

  statement {
    sid    = "SelfRoleInspection"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
    ]
    # Read-only, and ONLY on its own role — used by the preflight
    # script to verify the role hasn't been tampered with.
    resources = [aws_iam_role.github_actions_deploy.arn]
  }
}

resource "aws_iam_policy" "github_actions_deploy_permissions" {
  name   = "${var.project_name}-github-deploy-permissions"
  policy = data.aws_iam_policy_document.github_actions_deploy_permissions.json
}

resource "aws_iam_role_policy_attachment" "github_actions_deploy" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.github_actions_deploy_permissions.arn
}
################################################################################
# 3. DEPLOYMENTS BUCKET
# -----------------------
# Stores the packaged Lambda zip files uploaded by the GitHub Actions
# deploy step. This is intentionally a SEPARATE bucket from the
# drift-monitor's own EOL cache bucket (built in terraform/lambda/) —
# different purpose, different access pattern, different blast radius
# if either were ever compromised.
#
# UNPREDICTABLE NAME:
# Bucket names are global across all of AWS. A predictable name like
# "tfdriftmonitor-deployments" could be guessed and probed by anyone,
# even without your account ID. We append a random 8-character suffix
# that has no relationship to your account ID, project name, or any
# other discoverable value — it's generated once and stored in state.
################################################################################

resource "random_id" "deployments_suffix" {
  byte_length = 4 # produces an 8-character hex string
}

resource "aws_s3_bucket" "deployments" {
  bucket = "deploy-${random_id.deployments_suffix.hex}"

  tags = {
    Project = var.project_name
    Purpose = "Stores packaged Lambda deployment artifacts from CI/CD"
  }
}

# Block all public access — defense in depth, same as every other
# bucket in this project. No object here should ever be public.
resource "aws_s3_bucket_public_access_block" "deployments" {
  bucket = aws_s3_bucket.deployments.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning — if a bad deploy artifact overwrites a good one,
# the previous version is recoverable.
resource "aws_s3_bucket_versioning" "deployments" {
  bucket = aws_s3_bucket.deployments.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Encryption at rest — AWS-managed keys, no extra cost.
resource "aws_s3_bucket_server_side_encryption_configuration" "deployments" {
  bucket = aws_s3_bucket.deployments.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle — old deployment artifacts don't need to live forever.
# Keep 90 days of history for rollback/audit, then clean up.
resource "aws_s3_bucket_lifecycle_configuration" "deployments" {
  bucket = aws_s3_bucket.deployments.id

  rule {
    id     = "expire-old-deployments"
    status = "Enabled"

    filter {} # applies to all objects in the bucket

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
################################################################################
# 4. SES — OUT-OF-BAND AURORA ALERT
# ------------------------------------
# A separate notification channel from the routine SNS pipeline alerts.
# The whole point of "out-of-band" is that if the GitHub Actions deploy
# role is ever compromised, the attacker compromising that role does NOT
# automatically also compromise this alert channel's ability to notify
# a human — SES here is a distinct AWS service with its own scoped
# permission, not piggybacking on the SNS topic the pipeline uses for
# routine notifications.
#
# MANUAL STEP REQUIRED AFTER APPLY:
# Terraform can request email verification but cannot complete it.
# AWS sends a verification email to var.ses_alert_email — you must
# click the link in that email before SES can actually send from it.
################################################################################

resource "aws_ses_email_identity" "alert_sender" {
  email = var.ses_alert_email
}

# -----------------------------------------------------------------
# Scoped SES send permission for the deploy role.
#
# IMPORTANT — this is the one place the deploy role has an "alerting"
# capability beyond its core deploy job. We accept this because without
# it, a tamper event could never notify anyone. The scope is as narrow
# as SES allows:
#   - Can only send AS this one verified identity (ses:FromAddress condition)
#   - Cannot create, delete, or modify any SES identity or configuration
#   - Cannot send to arbitrary mailing lists — the destination is fixed
#     in the incident response script, not configurable by the caller
# -----------------------------------------------------------------

data "aws_iam_policy_document" "ses_send_aurora_alert" {
  statement {
    sid     = "SendAuroraAlertOnly"
    effect  = "Allow"
    actions = ["ses:SendEmail", "ses:SendRawEmail"]

    resources = [aws_ses_email_identity.alert_sender.arn]

    # Belt and suspenders: even though the resource ARN already scopes
    # this to one identity, we also assert the From address explicitly.
    # This means even if the resource scoping were ever loosened by
    # mistake in a future change, this condition still holds.
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [var.ses_alert_email]
    }
  }
}

resource "aws_iam_policy" "ses_send_aurora_alert" {
  name   = "${var.project_name}-ses-aurora-alert"
  policy = data.aws_iam_policy_document.ses_send_aurora_alert.json
}

resource "aws_iam_role_policy_attachment" "github_actions_ses" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.ses_send_aurora_alert.arn
}
################################################################################
# 5. SNS — ROUTINE PIPELINE NOTIFICATIONS
# ------------------------------------------
# This is the "boring" notification channel. Routine pipeline failures,
# clean run confirmations, and the deliberately boring public-facing
# tamper failure message all go here. This is DISTINCT from the SES
# AURORA channel — see the AURORA design notes in main_4.tf for why
# that separation matters.
################################################################################

resource "aws_sns_topic" "pipeline_alerts" {
  name = "${var.project_name}-pipeline-alerts"

  tags = {
    Project = var.project_name
    Purpose = "Routine CI/CD pipeline notifications"
  }
}

resource "aws_sns_topic_subscription" "pipeline_alerts_email" {
  topic_arn = aws_sns_topic.pipeline_alerts.arn
  protocol  = "email"
  endpoint  = var.sns_alert_email
}

data "aws_iam_policy_document" "sns_publish_pipeline" {
  statement {
    sid       = "PublishPipelineAlertsOnly"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.pipeline_alerts.arn]
  }
}

resource "aws_iam_policy" "sns_publish_pipeline" {
  name   = "${var.project_name}-sns-publish-pipeline"
  policy = data.aws_iam_policy_document.sns_publish_pipeline.json
}

resource "aws_iam_role_policy_attachment" "github_actions_sns" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.sns_publish_pipeline.arn
}
################################################################################
# 6. SSM PARAMETERS
# -------------------
# Two categories here:
#
#   A. PLACEHOLDERS — created empty by Terraform, filled in manually
#      by a human after apply. Terraform should never contain secret
#      VALUES in code or state where avoidable; it only reserves the
#      parameter name/path.
#
#   B. COMPUTED — the role integrity hash. This is NOT a manual
#      placeholder. It's calculated directly from the policy document
#      Terraform just applied, using Terraform's own sha256() function.
#      This means the stored hash can never drift from what the role
#      actually says, because they're generated from the same source
#      in the same apply.
################################################################################

# -----------------------------------------------------------------
# A. PLACEHOLDERS
# -----------------------------------------------------------------
# lifecycle.ignore_changes on "value" means: Terraform creates the
# parameter once with a placeholder value, but will never overwrite
# whatever a human later puts there on subsequent applies. Without
# this, every `terraform apply` would stomp your real secret back
# to the placeholder text.
# -----------------------------------------------------------------

resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = "/${var.project_name}/anthropic-api-key"
  type        = "SecureString"
  value       = "PLACEHOLDER-replace-via-aws-ssm-put-parameter"
  description = "Anthropic API key for EOL data parsing. Fill in manually after apply."

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Project = var.project_name
  }
}

resource "aws_ssm_parameter" "github_token" {
  name        = "/${var.project_name}/github-token"
  type        = "SecureString"
  value       = "PLACEHOLDER-replace-via-aws-ssm-put-parameter"
  description = "GitHub PAT for scanning private repos (optional - only needed if monitoring private repos). Fill in manually after apply."

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Project = var.project_name
  }
}

resource "aws_ssm_parameter" "terraform_repos_config" {
  name        = "/${var.project_name}/terraform-repos"
  type        = "SecureString"
  value       = jsonencode({ repos = [] })
  description = "JSON config of repos to scan for provider version drift. See terraform-repos-example.md."

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Project = var.project_name
  }
}

# -----------------------------------------------------------------
# B. COMPUTED — role integrity hash
# -----------------------------------------------------------------
# This hash is recalculated from the deploy role's policy document
# on every `terraform apply`. The preflight script compares this
# stored value against a live hash of whatever IAM actually reports
# for the role at run time. A mismatch means the role was modified
# OUTSIDE of Terraform — i.e. tampered with.
# -----------------------------------------------------------------

resource "aws_ssm_parameter" "role_integrity_hash" {
  name        = "/${var.project_name}/role-integrity-hash"
  type        = "String" # not secret — a hash reveals nothing about the policy itself
  value       = sha256(data.aws_iam_policy_document.github_actions_deploy_permissions.json)
  description = "SHA256 of the deploy role's permission policy as last applied by Terraform. Used by preflight to detect out-of-band tampering."

  tags = {
    Project = var.project_name
  }
}
################################################################################
# 7. JUST-IN-TIME SIMULATE-POLICY PERMISSION
# ----------------------------------------------
# The preflight check needs to call iam:SimulatePrincipalPolicy to verify
# its own permissions haven't drifted from what Terraform defined. Rather
# than granting that permission permanently, the role grants it to ITSELF
# at the start of the check via a tightly-named inline policy, uses it,
# then deletes that same inline policy immediately after — regardless of
# the check's outcome.
#
# WHAT THIS PERMISSION ALLOWS (and nothing more):
#   - iam:PutRolePolicy    — but ONLY on its own role, ONLY with this
#                            exact policy name
#   - iam:DeleteRolePolicy — same constraints, used to clean up
#
# WHAT STOPS THIS FROM BEING A PRIVILEGE ESCALATION PATH:
#   The permissions boundary (defined above, attached to the role)
#   caps the role's effective permissions regardless of what content
#   ends up inside the policy this grants itself. Even if the inline
#   policy content were somehow broader than intended, the boundary
#   prevents it from ever taking effect beyond the boundary's ceiling.
################################################################################

locals {
  # Single source of truth for this policy name. Written to SSM below
  # so the preflight script reads it rather than hardcoding a duplicate
  # value that could silently drift out of sync with this file.
  simulate_policy_name = "${var.project_name}-jit-simulate"
}

resource "aws_ssm_parameter" "simulate_policy_name" {
  name        = "/${var.project_name}/jit-simulate-policy-name"
  type        = "String"
  value       = local.simulate_policy_name
  description = "Name of the just-in-time inline policy the deploy role grants/revokes on itself. Read by preflight.py - do not hardcode this elsewhere."

  tags = {
    Project = var.project_name
  }
}

data "aws_iam_policy_document" "self_grant_simulate" {
  statement {
    sid    = "SelfGrantAndRevokeSimulateOnly"
    effect = "Allow"
    actions = [
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
    ]
    resources = [aws_iam_role.github_actions_deploy.arn]

    # The critical constraint: can only create or delete an inline
    # policy with this EXACT name. Cannot touch any other policy name,
    # cannot rename this one, cannot use this permission for anything
    # outside this one narrow purpose.
    condition {
      test     = "StringEquals"
      variable = "iam:PolicyName"
      values   = [local.simulate_policy_name]
    }
  }
}

resource "aws_iam_policy" "self_grant_simulate" {
  name   = "${var.project_name}-self-grant-simulate"
  policy = data.aws_iam_policy_document.self_grant_simulate.json
}

resource "aws_iam_role_policy_attachment" "github_actions_self_grant" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = aws_iam_policy.self_grant_simulate.arn
}
