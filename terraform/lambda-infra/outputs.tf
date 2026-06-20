################################################################################
# terraform/lambda-infra/outputs.tf
# -------------------------------------
# terraform/lambda-code/ doesn't read these outputs directly (no remote
# state sharing between the two roots) — it looks up the same
# underlying resources by NAME via its own data sources, same safe
# pattern used between bootstrap and this root. These outputs exist
# for human visibility and debugging, not as the actual wiring
# mechanism between the two roots.
################################################################################

output "lambda_role_name" {
  description = "Name of the Lambda execution role — looked up by terraform/lambda-code/ via data source"
  value       = aws_iam_role.drift_monitor.name
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role"
  value       = aws_iam_role.drift_monitor.arn
}

output "drift_findings_topic_arn" {
  description = "SNS topic ARN for drift-finding notifications"
  value       = aws_sns_topic.drift_findings.arn
}

output "analysis_cache_bucket" {
  description = "S3 bucket where AI changelog analysis is cached"
  value       = aws_s3_bucket.analysis_cache.bucket
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for Lambda run logs"
  value       = aws_cloudwatch_log_group.drift_monitor.name
}

output "post_apply_note" {
  description = "Reminder of what's still needed after this apply"
  value       = "Infrastructure ready. Next: apply terraform/lambda-code/ to create the actual Lambda function, which will look up this role, SNS topic, and bucket by name."
}
