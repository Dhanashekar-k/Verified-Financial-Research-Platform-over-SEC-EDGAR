variable "account_id" {
  description = "AWS account ID."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "lake_bucket_name" {
  description = "Name of the lake S3 bucket the jobs read and write."
  type        = string
}

variable "glue_database_names" {
  description = "Glue database names the jobs may use (raw and curated)."
  type        = list(string)
}

variable "log_retention_days" {
  description = "Retention for the Glue job log groups (04_aws_and_cost_plan.md §2 item 9)."
  type        = number
  default     = 7
}
