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

variable "glue_version" {
  description = "Glue runtime. 5.0 = Spark 3.5, Python 3.11, Iceberg 1.7."
  type        = string
  default     = "5.0"
}

variable "worker_type" {
  description = "Glue worker type. G.1X = 1 DPU (4 vCPU, 16 GB)."
  type        = string
  default     = "G.1X"
}

variable "number_of_workers" {
  description = "Workers per job run (billed as DPUs while the run lasts)."
  type        = number
  default     = 4
}

variable "timeout_minutes" {
  description = "A run is stopped after this many minutes."
  type        = number
  default     = 30
}
