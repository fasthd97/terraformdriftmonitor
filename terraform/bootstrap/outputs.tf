################################################################################
# terraform/bootstrap/outputs.tf
# ---------------------------------
# These outputs feed into two places:
#   1. GitHub repo variables/secrets (role ARN, bucket name)
#   2. terraform/lambda/ root, if it needs to reference bootstrap resources
#      (e.g. granting the Lambda's own role read access to a bootstrap
#      SSM parameter)
################################################################################

output "github_actions_role_arn" {
  description = "ARN of the role GitHub Actions assumes via OIDC. Set this as the AWS_ROLE_ARN repo variable."
  value       = aws_iam_role.github_actions_deploy.arn
}

output "deployments_bucket_name" {
  description = "Name of the S3 bucket where deployment artifacts are stored. Set this as the DEPLOYMENTS_BUCKET repo variable."
  value       = aws_s3_bucket.deployments.bucket
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider"
  value       = aws_iam_openid_connect_provider.github.arn
}

output "pipeline_sns_topic_arn" {
  description = "SNS topic ARN for routine pipeline notifications"
  value       = aws_sns_topic.pipeline_alerts.arn
}

output "ses_alert_identity_arn" {
  description = "SES identity ARN used for the out-of-band AURORA alert"
  value       = aws_ses_email_identity.alert_sender.arn
}

output "role_integrity_hash" {
  description = "Current SHA256 hash of the deploy role's policy — useful for manual verification"
  value       = aws_ssm_parameter.role_integrity_hash.value
  sensitive = true
}

output "post_apply_checklist" {
  description = "Manual steps required after this apply completes"
  value = <<-EOT
    ACTION REQUIRED after this apply:

    1. Check ${var.ses_alert_email} and confirm the SES identity verification email.
    2. Check ${var.sns_alert_email} and confirm the SNS subscription email.
    3. Fill in real values for the SSM placeholders:
       aws ssm put-parameter --name "/${var.project_name}/anthropic-api-key" --value "sk-ant-..." --type SecureString --overwrite
       aws ssm put-parameter --name "/${var.project_name}/github-token" --value "ghp_..." --type SecureString --overwrite (only if scanning private repos)
       aws ssm put-parameter --name "/${var.project_name}/terraform-repos" --value '{"repos":[...]}' --type SecureString --overwrite
    4. Set these as GitHub repo variables (Settings > Secrets and variables > Actions > Variables):
       AWS_ROLE_ARN = (see github_actions_role_arn output above)
       DEPLOYMENTS_BUCKET = (see deployments_bucket_name output above)
       AWS_REGION = ${var.aws_region}
  EOT
}
