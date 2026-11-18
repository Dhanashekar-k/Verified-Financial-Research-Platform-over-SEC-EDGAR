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

variable "release_label" {
  description = "EMR release label. Must equal the base image release in infra/emr/Dockerfile (ADR-024)."
  type        = string
  default     = "emr-7.14.0"
}

variable "image_uri" {
  description = "Custom image URI including the tag (<ecr repository_url>:<git-sha>). Empty for the stock image."
  type        = string
  default     = ""
}

variable "max_vcpu" {
  description = "Maximum vCPUs the application may use at once (the spend cap, M1 3.8)."
  type        = number
  default     = 64
}

variable "max_memory_gb" {
  description = "Maximum memory in GB the application may use at once."
  type        = number
  default     = 256
}

variable "max_disk_gb" {
  description = "Maximum ephemeral disk in GB the application may use at once."
  type        = number
  default     = 1000
}

variable "idle_timeout_minutes" {
  description = "Minutes of idleness before the application stops itself."
  type        = number
  default     = 5
}
