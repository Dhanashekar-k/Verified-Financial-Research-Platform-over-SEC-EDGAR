output "job_role_arn" {
  description = "Execution role ARN a Glue job assumes."
  value       = aws_iam_role.job.arn
}
