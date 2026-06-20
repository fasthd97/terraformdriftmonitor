################################################################################
# terraform/lambda-code/outputs.tf
# ------------------------------------
################################################################################

output "lambda_function_name" {
  description = "Name of the drift monitor Lambda function"
  value       = aws_lambda_function.drift_monitor.function_name
}

output "lambda_function_arn" {
  description = "ARN of the drift monitor Lambda function"
  value       = aws_lambda_function.drift_monitor.arn
}

output "manual_trigger_command" {
  description = "Paste into your terminal to trigger a manual run without waiting for the schedule"
  value       = "aws lambda invoke --function-name ${aws_lambda_function.drift_monitor.function_name} --payload '{\"manual\": true}' --cli-binary-format raw-in-base64-out response.json --region ${var.aws_region} && cat response.json"
}
