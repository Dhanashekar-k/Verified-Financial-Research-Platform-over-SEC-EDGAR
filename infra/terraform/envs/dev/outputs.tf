output "ecr_repository_url" {
  description = "Where `make emr-image-push` pushes the custom EMR image."
  value       = module.ecr.repository_url
}

output "emr_application_id" {
  description = "EMR Serverless application ID, for `make emr-run`."
  value       = module.emr.application_id
}

output "emr_job_role_arn" {
  description = "Execution role ARN for EMR job runs."
  value       = module.emr.job_role_arn
}

output "glue_job_role_arn" {
  description = "Execution role ARN for Glue job runs."
  value       = module.glue.job_role_arn
}

output "glue_job_names" {
  description = "Glue jobs, for `aws glue start-job-run --job-name`."
  value       = module.glue.job_names
}

output "serving_box_instance_id" {
  value = module.serving_db.instance_id
}
