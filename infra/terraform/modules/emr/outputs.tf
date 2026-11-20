output "application_id" {
  description = "EMR Serverless application ID, for `make emr-run`."
  value       = try(aws_emrserverless_application.spark[0].id, null)
}

output "job_role_arn" {
  description = "Execution role ARN a job run assumes."
  value       = aws_iam_role.job.arn
}
