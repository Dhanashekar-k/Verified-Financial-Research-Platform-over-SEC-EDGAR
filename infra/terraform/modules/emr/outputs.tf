output "application_id" {
  description = "EMR Serverless application ID, for `make emr-run`."
  value       = aws_emrserverless_application.spark.id
}

output "job_role_arn" {
  description = "Execution role ARN a job run assumes."
  value       = aws_iam_role.job.arn
}
