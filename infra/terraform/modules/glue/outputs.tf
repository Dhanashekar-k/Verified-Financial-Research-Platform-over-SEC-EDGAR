output "job_role_arn" {
  description = "Execution role ARN a Glue job assumes."
  value       = aws_iam_role.job.arn
}

output "job_names" {
  description = "Glue job names by script."
  value       = { for name, job in aws_glue_job.this : name => job.name }
}

output "smoke_database_names" {
  description = "Glue databases for smoke runs."
  value       = [for db in aws_glue_catalog_database.smoke : db.name]
}
